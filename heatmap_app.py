import ee
import geemap.foliumap as geemap
import folium
import json
import tempfile
import streamlit as st
from geopy.geocoders import Nominatim
from geopy.exc import GeocoderTimedOut

# EE auth
st.set_page_config(page_title="Urban Heat Risk Viewer", layout="wide")

try:
    with tempfile.NamedTemporaryFile(mode="w+", suffix=".json", delete=False) as f:
        json.dump(json.loads(st.secrets["earthengine"]["private_key"]), f)
        key_path = f.name
    credentials = ee.ServiceAccountCredentials(st.secrets["earthengine"]["service_account"], key_path)
    ee.Initialize(credentials)
except Exception as e:
    st.error("Earth Engine authentication failed.")
    st.stop()

# Geocoder
geolocator = Nominatim(user_agent="geoapi")
def geocode_with_retry(postcode, retries=3):
    for i in range(retries):
        try:
            return geolocator.geocode(postcode, timeout=10)
        except GeocoderTimedOut:
            if i == retries - 1:
                raise
            continue

# Layout
left_col, right_col = st.columns([1, 2])
Map = geemap.Map(center=[51.5, -0.1], zoom=10, basemap='SATELLITE')

with left_col:
    postcode = st.text_input("Enter UK Postcode:", value='SW1A 1AA')
    buffer_radius = st.slider("Buffer radius (meters)", 100, 2000, 500)
    selected_year = st.selectbox("Select Year", [str(y) for y in range(2013, 2025)])
    date_range = st.selectbox("Date Range", ['Full Year', 'Summer Only'])
    cloud_cover = st.slider("Cloud Cover Threshold (%)", 0, 50, 20)
    run_analysis = st.button("Run Urban Heat Risk")

if run_analysis:
    location = geocode_with_retry(postcode)
    if location:
        lat, lon = location.latitude, location.longitude
        point = ee.Geometry.Point([lon, lat])
        aoi = point.buffer(buffer_radius)

        start_date = f"{selected_year}-{'01-01' if date_range == 'Full Year' else '05-01'}"
        end_date = f"{selected_year}-{'12-31' if date_range == 'Full Year' else '08-31'}"

        def cloud_mask(image):
            qa = image.select('QA_PIXEL')
            return image.updateMask(qa.bitwiseAnd(1 << 3).Or(1 << 5).Not())

        IC = ee.ImageCollection("LANDSAT/LC08/C02/T1_TOA") \
            .filterDate(start_date, end_date) \
            .filterBounds(aoi) \
            .map(cloud_mask) \
            .filter(ee.Filter.lt('CLOUD_COVER', cloud_cover)) \
            .median()

        ndvi = IC.normalizedDifference(['B5', 'B4']).rename('NDVI')
        ndvi_stats = ndvi.reduceRegion(ee.Reducer.minMax().combine('mean', '', True), aoi, 30)

        ndvi_mean = ndvi_stats.get('NDVI_mean')
        ndvi_min = ndvi_stats.get('NDVI_min')
        ndvi_max = ndvi_stats.get('NDVI_max')

        thermal = IC.select('B10')
        fv = ndvi.subtract(ndvi_min).divide(ee.Number(ndvi_max).subtract(ndvi_min)).pow(2).rename('FV')
        em = fv.multiply(0.004).add(0.986).rename('EM')

        lst = thermal.expression(
            '(tb / (1 + (0.00115 * (tb / 0.4836)) * log(em))) - 273.15',
            {'tb': thermal.select('B10'), 'em': em}
        ).rename('LST')

        lst_mean = lst.reduceRegion(ee.Reducer.mean(), aoi, 30).get('LST')
        utfvi = lst.subtract(ee.Image.constant(lst_mean)).divide(lst).rename('UTFVI')
        utfvi_mean = utfvi.reduceRegion(ee.Reducer.mean(), aoi, 30).get('UTFVI')

        # Save if data exists
        try:
            st.session_state.map_center = [lat, lon]
            st.session_state.lst = lst.clip(aoi)
            st.session_state.utfvi = utfvi.clip(aoi)
            st.session_state.ndvi_mean = ndvi_mean.getInfo() if ndvi_mean else None
            st.session_state.lst_mean = lst_mean.getInfo() if lst_mean else None
            st.session_state.utfvi_mean = utfvi_mean.getInfo() if utfvi_mean else None
            st.session_state.utfvi_class = (
                "Excellent" if st.session_state.utfvi_mean is not None and st.session_state.utfvi_mean <= 0 else
                "Good" if st.session_state.utfvi_mean <= 0.005 else
                "Moderate" if st.session_state.utfvi_mean <= 0.015 else
                "Poor" if st.session_state.utfvi_mean <= 0.025 else
                "Ecological Risk"
            ) if st.session_state.utfvi_mean is not None else "Unknown"
        except Exception:
            st.warning("⚠️ Some data could not be computed.")

with right_col:
    st.markdown("### Heat Map Viewer")

    if "map_center" in st.session_state:
        Map.set_center(st.session_state.map_center[1], st.session_state.map_center[0], 16)
        Map.add_child(folium.Marker(
            location=st.session_state.map_center,
            icon=folium.Icon(color='red', icon='tint', prefix='fa'),
            popup=f"Postcode: {postcode}"
        ))

    show_lst = st.checkbox("Show LST", value=True)
    lst_opacity = st.slider("LST Opacity", 0.0, 1.0, 0.6)
    show_utfvi = st.checkbox("Show UTFVI", value=True)
    utfvi_opacity = st.slider("UTFVI Opacity", 0.0, 1.0, 0.6)

    if show_lst and "lst" in st.session_state:
        Map.addLayer(st.session_state.lst, {
            'min': 0, 'max': 56,
            'palette': ['darkblue', 'blue', 'lightblue', 'green', 'yellow', 'orange', 'red'],
            'opacity': lst_opacity
        }, 'LST')

    if show_utfvi and "utfvi" in st.session_state:
        Map.addLayer(st.session_state.utfvi, {
            'min': -0.4, 'max': 0.4,
            'palette': ['blue', 'green', 'yellow', 'orange', 'red'],
            'opacity': utfvi_opacity
        }, 'UTFVI')

    Map.to_streamlit(width=700, height=500, scrolling=True, add_layer_control=True)

with left_col.expander("Analysis Summary", expanded=True):
    if "ndvi_mean" in st.session_state and st.session_state.ndvi_mean is not None:
        st.write(f"### Mean NDVI: {st.session_state.ndvi_mean:.2f}")
        st.write(f"### Mean LST: {st.session_state.lst_mean:.2f} °C" if st.session_state.lst_mean else "LST not available")
        st.write(f"### Mean UTFVI: {st.session_state.utfvi_mean:.4f}" if st.session_state.utfvi_mean else "UTFVI not available")
        st.write(f"### Ecological Class: {st.session_state.utfvi_class}")

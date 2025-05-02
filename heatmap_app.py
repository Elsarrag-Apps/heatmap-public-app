import ee
import geemap.foliumap as geemap
import folium
import json
import tempfile
import streamlit as st
from geopy.geocoders import Nominatim
from geopy.exc import GeocoderTimedOut

# Earth Engine authentication using Streamlit secrets
try:
    with tempfile.NamedTemporaryFile(mode="w+", suffix=".json", delete=False) as f:
        json.dump(json.loads(st.secrets["earthengine"]["private_key"]), f)
        key_path = f.name
    credentials = ee.ServiceAccountCredentials(st.secrets["earthengine"]["service_account"], key_path)
    ee.Initialize(credentials)
except Exception as e:
    st.error("Earth Engine authentication failed. Check Streamlit secrets.")
    st.stop()

st.set_page_config(page_title="Urban Heat Risk Viewer", layout="wide")

# Top logos
st.markdown("""
<style>
.top-container {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 10px;
}
</style>
<div class='top-container'>
    <a href="https://www.ukgbc.org" target="_blank">
        <img src="https://upload.wikimedia.org/wikipedia/en/2/29/UK_Green_Building_Council_logo.png" width="120"/>
    </a>
    <a href="https://www.hoarelea.com" target="_blank">
        <img src="https://upload.wikimedia.org/wikipedia/en/thumb/2/28/Hoare_Lea_logo.svg/320px-Hoare_Lea_logo.svg.png" width="120"/>
    </a>
</div>
""", unsafe_allow_html=True)

# Layout
left_col, right_col = st.columns([1, 2])

with left_col:
    postcode = st.text_input("Enter UK Postcode:", value='SW1A 1AA')
    buffer_radius = st.slider("Buffer radius (meters)", 100, 2000, 500)
    selected_year = st.selectbox("Select Year", [str(y) for y in range(2013, 2025)])
    date_range = st.selectbox("Date Range", ['Full Year', 'Summer Only'])
    cloud_cover = st.slider("Cloud Cover Threshold (%)", 0, 50, 20)
    run_analysis = st.button("Run Analysis")

geolocator = Nominatim(user_agent="geoapi")

def geocode_with_retry(postcode, retries=3):
    for i in range(retries):
        try:
            return geolocator.geocode(postcode, timeout=10)
        except GeocoderTimedOut:
            if i == retries - 1:
                raise
            continue

if run_analysis:
    location = geocode_with_retry(postcode)
    if location is None:
        st.error(f"Invalid postcode: {postcode}")
        st.stop()

    lat, lon = location.latitude, location.longitude
    point = ee.Geometry.Point([lon, lat])
    aoi = point.buffer(buffer_radius)
  
    start_date = f"{selected_year}-{'01-01' if date_range == 'Full Year' else '05-01'}"
    end_date = f"{selected_year}-{'12-31' if date_range == 'Full Year' else '08-31'}"

    def cloud_mask(image):
        qa = image.select('QA_PIXEL')
        mask = qa.bitwiseAnd(1 << 3).Or(qa.bitwiseAnd(1 << 5))
        return image.updateMask(mask.Not())

    IC = ee.ImageCollection("LANDSAT/LC08/C02/T1_TOA") \
        .filterDate(start_date, end_date) \
        .filterBounds(aoi) \
        .map(cloud_mask) \
        .filter(ee.Filter.lt('CLOUD_COVER', cloud_cover)) \
        .mean()
   
      
    ndvi = IC.normalizedDifference(['B5', 'B4']).rename('NDVI')
    ndvi_stats = ndvi.reduceRegion(ee.Reducer.minMax().combine('mean', '', True), aoi, 30)
    ndvi_mean = ee.Number(ndvi_stats.get('NDVI_mean'))
    ndvi_min = ee.Number(ndvi_stats.get('NDVI_min'))
    ndvi_max = ee.Number(ndvi_stats.get('NDVI_max'))

    thermal = IC.select('B10')
    fv = ndvi.subtract(ndvi_min).divide(ndvi_max.subtract(ndvi_min)).pow(2).rename('FV')
    em = fv.multiply(0.004).add(0.986).rename('EM')

    lst = thermal.expression(
        '(tb / (1 + (0.00115 * (tb / 0.4836)) * log(em))) - 273.15',
        {'tb': thermal.select('B10'), 'em': em}
    ).rename('LST')
    lst_mean = lst.reduceRegion(ee.Reducer.mean(), aoi, 30).get('LST')

    utfvi = lst.subtract(ee.Image.constant(lst_mean)).divide(lst).rename('UTFVI')
    utfvi_mean = utfvi.reduceRegion(ee.Reducer.mean(), aoi, 30).get('UTFVI')

    st.session_state.map_center = [lat, lon]
    st.session_state.lst = lst
    st.session_state.utfvi = utfvi
    st.session_state.ndvi_mean = ndvi_mean.getInfo()
    st.session_state.lst_mean = lst_mean.getInfo()
    st.session_state.utfvi_mean = utfvi_mean.getInfo()
    st.session_state.utfvi_class = (
        "Excellent" if st.session_state.utfvi_mean <= 0 else
        "Good" if st.session_state.utfvi_mean <= 0.005 else
        "Moderate" if st.session_state.utfvi_mean <= 0.01 else
        "Poor" if st.session_state.utfvi_mean <= 0.015 else
        "Very Poor" if st.session_state.utfvi_mean <= 0.02 else
        "Worst"
    )

# Always-visible map viewer
with right_col:
    st.markdown("### Heat Map Viewer")
    Map = geemap.Map(center=[51.5, -0.1], zoom=10, basemap='SATELLITE')

    if "map_center" in st.session_state:
        Map.set_center(st.session_state.map_center[1], st.session_state.map_center[0], 16)
        Map.add_child(folium.Marker(
            location=st.session_state.map_center,
            icon=folium.Icon(color='red', icon='tint', prefix='fa'),
            popup=f"Postcode: {postcode}"
        ))

    # Layer controls
    show_lst = st.checkbox("Show LST", value=True)
    lst_opacity = st.slider("LST Opacity", 0.0, 1.0, 0.6)
    show_utfvi = st.checkbox("Show UTFVI", value=True)
    utfvi_opacity = st.slider("UTFVI Opacity", 0.0, 1.0, 0.6)

    if "lst" in st.session_state and show_lst:
        classified_lst = st.session_state.lst \
            .where(st.session_state.lst.lte(20), 0) \
            .where(st.session_state.lst.gt(20).And(st.session_state.lst.lte(26)), 1) \
            .where(st.session_state.lst.gt(26).And(st.session_state.lst.lte(33)), 2) \
            .where(st.session_state.lst.gt(33).And(st.session_state.lst.lte(38)), 3) \
            .where(st.session_state.lst.gt(38).And(st.session_state.lst.lte(44)), 4) \
            .where(st.session_state.lst.gt(44).And(st.session_state.lst.lte(50)), 5) \
            .where(st.session_state.lst.gt(50), 6)

        lst_palette = ['#08306B', '#2171B5', '#41AB5D', '#D9F0A3', '#FECC5C', '#FD8D3C', '#E31A1C']

        Map.addLayer(classified_lst, {
            'min': 0, 'max': 6,
            'palette': lst_palette,
            'opacity': lst_opacity
        }, 'LST Classes')

    if "utfvi" in st.session_state and show_utfvi:
        classified_utfvi = st.session_state.utfvi \
            .where(st.session_state.utfvi.lte(0), 0) \
            .where(st.session_state.utfvi.gt(0).And(st.session_state.utfvi.lte(0.005)), 1) \
            .where(st.session_state.utfvi.gt(0.005).And(st.session_state.utfvi.lte(0.01)), 2) \
            .where(st.session_state.utfvi.gt(0.01).And(st.session_state.utfvi.lte(0.015)), 3) \
            .where(st.session_state.utfvi.gt(0.015).And(st.session_state.utfvi.lte(0.02)), 4) \
            .where(st.session_state.utfvi.gt(0.02), 5)

        utfvi_palette = ['#08306B', '#31A354', '#FFFF66', '#FDAE61', '#F03B20', '#BD0026']

        Map.addLayer(classified_utfvi, {
            'min': 0, 'max': 5,
            'palette': utfvi_palette,
            'opacity': utfvi_opacity
        }, 'UTFVI Classes')

    Map.to_streamlit(width=700, height=500, scrolling=True, add_layer_control=True)

# Summary Panel
with left_col.expander("Analysis Summary", expanded=True):
    if "ndvi_mean" in st.session_state:
        st.write(f"### Mean NDVI: {st.session_state.ndvi_mean:.2f}")
        st.write(f"### Mean LST: {st.session_state.lst_mean:.2f} °C")
        st.write(f"### Mean UTFVI: {st.session_state.utfvi_mean:.4f}")
        st.write(f"### Ecological Class: {st.session_state.utfvi_class}")

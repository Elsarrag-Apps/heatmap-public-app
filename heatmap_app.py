import ee
import geemap.foliumap as geemap
import folium
import json
import tempfile
import streamlit as st
from geopy.geocoders import Nominatim
from geopy.exc import GeocoderTimedOut

# Streamlit page setup
st.set_page_config(page_title="Urban Heat Risk Viewer", layout="wide")

# Mode selector
mode = st.radio("Select View Mode", ["Urban Heat Risk", "Building Overheating Risk"])

# Shared layout and map object
left_col, right_col = st.columns([1, 2])
Map = geemap.Map(center=[51.5, -0.1], zoom=10, basemap='SATELLITE')

# Earth Engine authentication
try:
    with tempfile.NamedTemporaryFile(mode="w+", suffix=".json", delete=False) as f:
        json.dump(json.loads(st.secrets["earthengine"]["private_key"]), f)
        key_path = f.name
    credentials = ee.ServiceAccountCredentials(st.secrets["earthengine"]["service_account"], key_path)
    ee.Initialize(credentials)
except Exception:
    st.error("Earth Engine authentication failed. Check Streamlit secrets.")
    st.stop()

# Global geocoder
geolocator = Nominatim(user_agent="geoapi")
def geocode_with_retry(postcode, retries=3):
    for i in range(retries):
        try:
            return geolocator.geocode(postcode, timeout=10)
        except GeocoderTimedOut:
            if i == retries - 1:
                raise
            continue

# Mode 1: Urban Heat Risk
if mode == "Urban Heat Risk":

    with left_col:
        postcode = st.text_input("Enter UK Postcode:", value='SW1A 1AA', key="postcode_urban")
        buffer_radius = st.slider("Buffer radius (meters)", 100, 2000, 500)
        selected_year = st.selectbox("Select Year", [str(y) for y in range(2013, 2025)])
        date_range = st.selectbox("Date Range", ['Full Year', 'Summer Only'])
        cloud_cover = st.slider("Cloud Cover Threshold (%)", 0, 50, 20)
        run_analysis = st.button("Run Urban Heat Risk")

    if run_analysis:
        location = geocode_with_retry(postcode)
        if location is None:
            st.error("Invalid postcode.")
            st.stop()

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
        ndvi_mean = ee.Number(ndvi_stats.get('NDVI_mean'))
        thermal = IC.select('B10')
        fv = ndvi.subtract(ndvi_stats.get('NDVI_min')).divide(
            ee.Number(ndvi_stats.get('NDVI_max')).subtract(ndvi_stats.get('NDVI_min'))).pow(2)
        em = fv.multiply(0.004).add(0.986)

        lst = thermal.expression(
            '(tb / (1 + (0.00115 * (tb / 0.4836)) * log(em))) - 273.15',
            {'tb': thermal.select('B10'), 'em': em}
        ).rename('LST')

        lst_mean = lst.reduceRegion(ee.Reducer.mean(), aoi, 30).get('LST')
        utfvi = lst.subtract(ee.Image.constant(lst_mean)).divide(lst).rename('UTFVI')
        utfvi_mean = utfvi.reduceRegion(ee.Reducer.mean(), aoi, 30).get('UTFVI')

        st.session_state.map_center = [lat, lon]
        st.session_state.lst = lst.clip(aoi)
        st.session_state.utfvi = utfvi.clip(aoi)
        st.session_state.ndvi_mean = ndvi_mean.getInfo()
        st.session_state.lst_mean = lst_mean.getInfo()
        st.session_state.utfvi_mean = utfvi_mean.getInfo()
        st.session_state.utfvi_class = (
            "Excellent" if st.session_state.utfvi_mean <= 0 else
            "Good" if st.session_state.utfvi_mean <= 0.005 else
            "Moderate" if st.session_state.utfvi_mean <= 0.015 else
            "Poor" if st.session_state.utfvi_mean <= 0.025 else
            "Ecological Risk"
        )

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

        if "lst" in st.session_state and show_lst:
            Map.addLayer(st.session_state.lst, {
                'min': 0, 'max': 56,
                'palette': ['darkblue', 'blue', 'lightblue', 'green', 'yellow', 'orange', 'red'],
                'opacity': lst_opacity
            }, 'LST')

        if "utfvi" in st.session_state and show_utfvi:
            Map.addLayer(st.session_state.utfvi, {
                'min': -0.4, 'max': 0.4,
                'palette': ['blue', 'green', 'yellow', 'orange', 'red'],
                'opacity': utfvi_opacity
            }, 'UTFVI')

    with left_col.expander("Analysis Summary", expanded=True):
        if "ndvi_mean" in st.session_state:
            st.write("### Mean NDVI: {:.2f}".format(st.session_state.ndvi_mean))
            st.write("### Mean LST: {:.2f} °C".format(st.session_state.lst_mean))
            st.write("### Mean UTFVI: {:.4f}".format(st.session_state.utfvi_mean))
            st.write("### Ecological Class: {}".format(st.session_state.utfvi_class))

# Mode 2: Building Overheating Risk
elif mode == "Building Overheating Risk":
    st.markdown("### 🏢 Building Overheating Risk Tool")

    with left_col:
        postcode_b = st.text_input("Enter UK Postcode", value="SW1A 1AA", key="postcode_building")
        locate = st.button("Check Overheating Zone")

        building_type = st.selectbox("Select Building Type", [
            "Low-Rise Residential", "High-Rise Residential", "Office",
            "School", "Care Home", "Healthcare"
        ])
        age_band = st.selectbox("Select Building Age Band", [
            "Pre-1945", "1945–1970", "1970–2000", "2000–2020", "New Build"
        ])
        mitigation = st.radio("Select Mitigation Strategy", ["Baseline", "Passive", "Active"])

    if locate:
        location_b = geocode_with_retry(postcode_b)
        if location_b:
            lat_b, lon_b = location_b.latitude, location_b.longitude
            user_point = ee.Geometry.Point([lon_b, lat_b])
            city_coords = {
                "Leeds": (53.8008, -1.5491),
                "Nottingham": (52.9548, -1.1581),
                "London": (51.5074, -0.1278),
                "Glasgow": (55.8642, -4.2518),
                "Cardiff": (51.4816, -3.1791),
                "Swindon": (51.5558, -1.7797)
            }
            city_buffers = {
                city: ee.Geometry.Point([lon, lat]).buffer(150000)
                for city, (lat, lon) in city_coords.items()
            }

            matched_city = None
            for city, buffer_geom in city_buffers.items():
                if buffer_geom.contains(user_point).getInfo():
                    matched_city = city
                    break

            if matched_city:
                st.success(f"📍 Postcode matches to: **{matched_city}**")
                st.session_state.selected_city = matched_city
                st.session_state.user_coords = (lat_b, lon_b)
                Map.set_center(lon_b, lat_b, 10)
                Map.add_child(folium.Marker(
                    location=(lat_b, lon_b),
                    icon=folium.Icon(color="red", icon="home", prefix="fa"),
                    popup=f"{matched_city} – {building_type}"
                ))
            else:
                st.warning("⚠️ This postcode is outside the known analysis zones.")
                st.session_state.selected_city = None
        else:
            st.error("Postcode could not be geolocated.")

# ✅ Final shared map display
with right_col:
    Map.to_streamlit(width=700, height=500, scrolling=True, add_layer_control=True)

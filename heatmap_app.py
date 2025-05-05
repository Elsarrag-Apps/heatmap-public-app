import ee
import geemap.foliumap as geemap
import folium
import json
import tempfile
import streamlit as st
from geopy.geocoders import Nominatim
from geopy.exc import GeocoderTimedOut

# Setup
st.set_page_config(page_title="Urban Heat Risk Viewer", layout="wide")
mode = st.radio("Select View Mode", ["Urban Heat Risk", "Building Overheating Risk"], key="mode_selector")

# EE Auth
try:
    with tempfile.NamedTemporaryFile(mode="w+", suffix=".json", delete=False) as f:
        json.dump(json.loads(st.secrets["earthengine"]["private_key"]), f)
        key_path = f.name
    credentials = ee.ServiceAccountCredentials(st.secrets["earthengine"]["service_account"], key_path)
    ee.Initialize(credentials)
except Exception:
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

# Shared Layout
left_col, right_col = st.columns([1, 2])
Map = geemap.Map(center=[51.5, -0.1], zoom=10, basemap='SATELLITE')

# === Urban Heat Risk ===
def run_urban_heat_risk(left_col, right_col, Map):
    with left_col:
        postcode = st.text_input("Enter UK Postcode:", value='SW1A 1AA', key="postcode_urban")
        buffer_radius = st.slider("Buffer radius (meters)", 100, 2000, 500, key="urban_buffer")
        selected_year = st.selectbox("Select Year", [str(y) for y in range(2013, 2025)], key="urban_year")
        date_range = st.selectbox("Date Range", ['Full Year', 'Summer Only'], key="urban_daterange")
        cloud_cover = st.slider("Cloud Cover Threshold (%)", 0, 50, 20, key="urban_cloud")
        run_analysis = st.button("Run Analysis", key="urban_run_btn")

    if run_analysis:
        location = geocode_with_retry(postcode)
        if location:
            lat, lon = location.latitude, location.longitude
            point = ee.Geometry.Point([lon, lat])
            aoi = point.buffer(buffer_radius)
            st.session_state.map_center = [lat, lon]

            start_date = f"{selected_year}-{'01-01' if date_range == 'Full Year' else '05-01'}"
            end_date = f"{selected_year}-{'12-31' if date_range == 'Full Year' else '08-31'}"

            def cloud_mask(image):
                qa = image.select('QA_PIXEL')
                return image.updateMask(qa.bitwiseAnd(1 << 3).Or(1 << 5).Not())

            IC = ee.ImageCollection("LANDSAT/LC08/C02/T1_TOA") \
                .filterBounds(aoi) \
                .filter(ee.Filter.lt('CLOUD_COVER', cloud_cover)) \
                .filterDate(start_date, end_date) \
                .median()

            ndvi = IC.normalizedDifference(['B5', 'B4']).rename('NDVI')
            thermal = IC.select('B10')
            fv = ndvi.pow(2)
            em = fv.multiply(0.004).add(0.986)

            lst = thermal.expression(
                '(tb / (1 + (0.00115 * (tb / 0.4836)) * log(em))) - 273.15',
                {'tb': thermal.select('B10'), 'em': em}
            ).rename("LST")

            st.session_state.lst = lst.clip(aoi)

    with right_col:
        if "map_center" in st.session_state:
            Map.set_center(st.session_state.map_center[1], st.session_state.map_center[0], 13)

        if "lst" in st.session_state:
            Map.addLayer(st.session_state.lst, {
                "min": 280, "max": 320,
                "palette": ["blue", "green", "yellow", "orange", "red"]
            }, "LST")

        Map.to_streamlit(width=700, height=500, scrolling=True, add_layer_control=True)

# === Building Overheating Risk ===
def run_building_overheating_risk(left_col, right_col, Map):
    with left_col:
        st.markdown("## 🏢 Building Overheating Risk Tool")

        postcode_b = st.text_input("Enter UK Postcode", value="SW1A 1AA", key="postcode_building")
        locate = st.button("Locate and Analyze", key="locate_btn")

        building_type = st.selectbox("Select Building Type", [
            "Low-Rise Residential", "High-Rise Residential", "Office",
            "School", "Care Home", "Healthcare"
        ], key="bldg_type")

        age_band = st.selectbox("Select Building Age Band", [
            "Pre-1945", "1945–1970", "1970–2000", "2000–2020", "New Build"
        ], key="bldg_age")

        mitigation = st.radio("Mitigation Strategy", ["Baseline", "Passive", "Active"], key="bldg_mitigation")

    if locate:
        location_b = geocode_with_retry(postcode_b)
        if location_b:
            lat_b, lon_b = location_b.latitude, location_b.longitude
            point = ee.Geometry.Point([lon_b, lat_b])
            st.session_state.user_coords = (lat_b, lon_b)

            city_coords = {
                "Leeds": (53.8008, -1.5491),
                "Nottingham": (52.9548, -1.1581),
                "London": (51.5074, -0.1278),
                "Glasgow": (55.8642, -4.2518),
                "Cardiff": (51.4816, -3.1791),
                "Swindon": (51.5558, -1.7797)
            }

            matched_city = None
            try:
                for city, (lat_c, lon_c) in city_coords.items():
                    buffer = ee.Geometry.Point([lon_c, lat_c]).buffer(200000)
                    if buffer.contains(point).getInfo():
                        matched_city = city
                        break
            except Exception as e:
                st.error(f"Error checking city buffers: {e}")
                return

            if matched_city:
                st.success(f"📌 Matched to: {matched_city}")
                st.session_state.selected_city = matched_city

            try:
                display_circle = ee.Geometry.Point([lon_b, lat_b]).buffer(50)
                st.session_state.display_circle = display_circle
            except Exception as e:
                st.warning(f"⚠️ Failed to create map circle: {e}")

    with right_col:
        st.markdown("### Building Risk Map")
        try:
            if "user_coords" in st.session_state:
                lat, lon = st.session_state.user_coords
                Map.set_center(lon, lat, 15)
            if "display_circle" in st.session_state:
                Map.addLayer(st.session_state.display_circle, {"color": "orange"}, "Selected Site")
            Map.to_streamlit(width=700, height=500, scrolling=True, add_layer_control=True)
        except Exception as e:
            st.error(f"🚨 Map error: {e}")

# === Dispatcher ===
if mode == "Urban Heat Risk":
    run_urban_heat_risk(left_col, right_col, Map)

elif mode == "Building Overheating Risk":
    run_building_overheating_risk(left_col, right_col, Map)

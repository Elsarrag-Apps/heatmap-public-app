import ee
import geemap.foliumap as geemap
import folium
import json
import tempfile
import streamlit as st
from geopy.geocoders import Nominatim
from geopy.exc import GeocoderTimedOut
from geopy.distance import geodesic


# Page setup
st.set_page_config(page_title="Urban Heat Risk Viewer", layout="wide")
mode = st.radio("Select View Mode", ["Urban Heat Risk", "Building Overheating Risk"], key="mode_selector")

# EE auth
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

# Layout
left_col, right_col = st.columns([1, 2])
Map = geemap.Map(center=[51.5, -0.1], zoom=10, basemap='SATELLITE')

# -------------------------------
# MODE 1: Urban Heat Risk (verified)
# -------------------------------
if mode == "Urban Heat Risk":
    with left_col:
        postcode = st.text_input("Enter UK Postcode:", value='SW1A 1AA', key="postcode_urban")
        buffer_radius = st.slider("Buffer radius (meters)", 100, 2000, 500, key="urban_buf")
        selected_year = st.selectbox("Select Year", [str(y) for y in range(2013, 2025)], key="urban_year")
        date_range = st.selectbox("Date Range", ['Full Year', 'Summer Only'], key="urban_daterange")
        cloud_cover = st.slider("Cloud Cover Threshold (%)", 0, 50, 20, key="urban_cloud")
        run_analysis = st.button("Run Analysis", key="urban_run")

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
            .median()

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
        Map = geemap.Map(center=[51.5, -0.1], zoom=10, basemap='SATELLITE')

        if "map_center" in st.session_state:
            Map.set_center(st.session_state.map_center[1], st.session_state.map_center[0], 16)
            Map.add_child(folium.Marker(
                location=st.session_state.map_center,
                icon=folium.Icon(color='red', icon='tint', prefix='fa'),
                popup=f"Postcode: {postcode}"
            ))

        show_lst = st.checkbox("Show LST", value=True, key="show_lst")
        lst_opacity = st.slider("LST Opacity", 0.0, 1.0, 0.6, key="lst_opacity")
        show_utfvi = st.checkbox("Show UTFVI", value=True, key="show_utfvi")
        utfvi_opacity = st.slider("UTFVI Opacity", 0.0, 1.0, 0.6, key="utfvi_opacity")

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

        Map.to_streamlit(width=700, height=500, scrolling=True, add_layer_control=True)

    with left_col.expander("Analysis Summary", expanded=True):
        if "ndvi_mean" in st.session_state:
            st.write(f"### Mean NDVI: {st.session_state.ndvi_mean:.2f}")
            st.write(f"### Mean LST: {st.session_state.lst_mean:.2f} °C")
            st.write(f"### Mean UTFVI: {st.session_state.utfvi_mean:.4f}")
            st.write(f"### Ecological Class: {st.session_state.utfvi_class}")
# -------------------------------
# MODE 2: Building Overheating Risk
# -------------------------------
elif mode == "Building Overheating Risk":
    from risk_data_office import risk_data_office
    from geopy.distance import geodesic

    def run_building_overheating_risk(left_col, right_col, Map):
        risk_categories = {
            1: {"label": "Low", "color": "green"},
            2: {"label": "Medium", "color": "yellow"},
            3: {"label": "High", "color": "orange"},
            4: {"label": "Very High", "color": "red"},
            5: {"label": "Extreme", "color": "darkred"}
        }

        risk_legend_html = """
        <div style="padding:10px">
          <h4 style="margin-bottom:5px">Risk Level Legend</h4>
          <div style="display:flex;flex-direction:column;font-size:14px">
            <div><span style="display:inline-block;width:15px;height:15px;background-color:green;margin-right:6px;"></span>Low</div>
            <div><span style="display:inline-block;width:15px;height:15px;background-color:yellow;margin-right:6px;"></span>Medium</div>
            <div><span style="display:inline-block;width:15px;height:15px;background-color:orange;margin-right:6px;"></span>High</div>
            <div><span style="display:inline-block;width:15px;height:15px;background-color:red;margin-right:6px;"></span>Very High</div>
            <div><span style="display:inline-block;width:15px;height:15px;background-color:darkred;margin-right:6px;"></span>Extreme</div>
          </div>
        </div>
        """

        with left_col:
            st.markdown("## 🏢 Building Overheating Risk Tool")
            postcode_b = st.text_input("Enter UK Postcode", value="SW1A 1AA", key="postcode_building")
            building_type = st.selectbox("Building Type", ["Office"], key="btype")
            age_band = st.selectbox("Age Band", ["Pre-1945", "1945–1970", "1970–2000", "2000–2020", "New Build"], key="ageband")
            mitigation = st.radio("Mitigation", ["Baseline", "Passive", "Active"], key="mitigation")
            climate = st.selectbox("Climate Scenario", ["2°C", "3°C", "4°C"], key="climate")

        location_b = geocode_with_retry(postcode_b)
        if location_b:
            lat_b, lon_b = location_b.latitude, location_b.longitude
            postcode_coords = (lat_b, lon_b)

            city_coords = {
                "Leeds": (53.8008, -1.5491),
                "Nottingham": (52.9548, -1.1581),
                "London": (51.5074, -0.1278),
                "Glasgow": (55.8642, -4.2518),
                "Cardiff": (51.4816, -3.1791),
                "Swindon": (51.5558, -1.7797)
            }

            matched_city, distance_km = min(
                ((city, geodesic(postcode_coords, coords).km) for city, coords in city_coords.items()),
                key=lambda x: x[1]
            )

            st.success(f"📌 Nearest city: {matched_city} ({distance_km:.1f} km)")

            entry = risk_data.get(matched_city, {}).get(building_type, {}).get(age_band, {}).get(mitigation, {}).get(climate)
            if entry:
                level = entry["level"]
                label = risk_categories[level]["label"]
                scenario = entry["scenario"]
                color = risk_categories[level]["color"]

                st.markdown(f"<div style='font-size:18px;'><strong>🛑 Risk Level {level} – {label}</strong><br><em>{scenario}</em></div>", unsafe_allow_html=True)

                circle = ee.Geometry.Point([lon_b, lat_b]).buffer(50)
                Map.set_center(lon_b, lat_b, 15)
                Map.addLayer(circle, {"color": color}, "Risk Circle")
            else:
                st.warning("❌ No risk data found for this selection.")

        with right_col:
            st.markdown("### Risk Map")
            Map.to_streamlit(width=700, height=500, scrolling=True, add_layer_control=True)
            st.markdown(risk_legend_html, unsafe_allow_html=True)

    run_building_overheating_risk(left_col, right_col, Map)





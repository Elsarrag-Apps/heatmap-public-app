import ee
import geemap.foliumap as geemap
import folium
from geopy.geocoders import Nominatim

# Check if running inside Streamlit or fallback to console mode
try:
    import streamlit as st
    STREAMLIT_MODE = True
except ModuleNotFoundError:
    print("Streamlit not found. Running in Jupyter fallback mode.")
    STREAMLIT_MODE = False

# Initialize Earth Engine from secrets using temp file
import json
import tempfile

with tempfile.NamedTemporaryFile(mode="w+", delete=False, suffix=".json") as f:
    json.dump(json.loads(st.secrets["earthengine"]["private_key"]), f)
    key_path = f.name

credentials = ee.ServiceAccountCredentials(
    st.secrets["earthengine"]["service_account"],
    key_path
)
ee.Initialize(credentials)
except Exception as e:
    ee.Authenticate()
    ee.Initialize()

# Default parameters
postcode = 'SW1A 1AA'
if STREAMLIT_MODE:
    # Two-column layout
    left_col, right_col = st.columns([1, 2])

    with left_col:
        postcode = st.text_input("Enter UK Postcode:", value='SW1A 1AA')
        buffer_radius = st.slider("Buffer radius (meters)", min_value=100, max_value=2000, value=500, step=100, key='input_buffer')
        selected_year = st.selectbox("Select Year", [str(y) for y in range(2013, 2025)], index=9, key='input_year')
        date_range = st.selectbox("Date Range", ['Full Year', 'Summer Only'], key='input_season')
        cloud_cover = st.slider("Cloud Cover Threshold (%)", 0, 50, 20, key='input_cloud')
        run_analysis = st.button("Run Analysis")

    with right_col:
        st.markdown("## Map Output")

geolocator = Nominatim(user_agent="geoapi")
from geopy.exc import GeocoderTimedOut

def geocode_with_retry(postcode, retries=3):
    for i in range(retries):
        try:
            return geolocator.geocode(postcode, timeout=10)
        except GeocoderTimedOut:
            if i == retries - 1:
                raise
            continue

# Run analysis only when user clicks the button
if STREAMLIT_MODE and run_analysis:
    location = geocode_with_retry(postcode)
    if 'location' not in locals() or location is None:
        st.error(f"Invalid or unreachable postcode: {postcode}. Please check your input or connection and try again.")
        st.stop()

    lat, lon = location.latitude, location.longitude
    point = ee.Geometry.Point([lon, lat])
    aoi = point.buffer(buffer_radius)

    Map = geemap.Map(center=[lat, lon], zoom=16, basemap='SATELLITE')
    Map.add_child(folium.Marker(
        location=[lat, lon],
        icon=folium.Icon(color='red', icon='tint', prefix='fa'),
        popup=f"Postcode: {postcode}"
    ))

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
    ndvi_min = ee.Number(ndvi_stats.get('NDVI_min'))
    ndvi_max = ee.Number(ndvi_stats.get('NDVI_max'))
    ndvi_mean = ee.Number(ndvi_stats.get('NDVI_mean'))

    thermal = IC.select('B10')
    fv = ndvi.subtract(ndvi_min).divide(ndvi_max.subtract(ndvi_min)).pow(2).rename('FV')
    em = fv.multiply(0.004).add(0.986).rename('EM')

    lst = thermal.expression(
        '(tb / (1 + (0.00115 * (tb / 0.4836)) * log(em))) - 273.15',
        {
            'tb': thermal.select('B10'),
            'em': em
        }
    ).rename('LST')
    lst_mean = lst.reduceRegion(ee.Reducer.mean(), aoi, 30).get('LST')

    utfvi = lst.subtract(ee.Image.constant(lst_mean)).divide(lst).rename('UTFVI')
    utfvi_mean = utfvi.reduceRegion(ee.Reducer.mean(), aoi, 30).get('UTFVI')

    def classify_utfvi(value):
        if value <= 0:
            return "Excellent"
        elif value <= 0.005:
            return "Good"
        elif value <= 0.015:
            return "Moderate"
        elif value <= 0.025:
            return "Poor"
        else:
            return "Ecological Risk"

    with right_col:
        st.markdown("### Heat Map Viewer")
    with st.expander("Map Layers", expanded=True):
        show_lst = st.checkbox("Show LST", value=True)
        lst_opacity = st.slider("LST Layer Opacity", 0.0, 1.0, 0.6, key='layer_lst_opacity')
        show_utfvi = st.checkbox("Show UTFVI", value=True)
        utfvi_opacity = st.slider("UTFVI Layer Opacity", 0.0, 1.0, 0.6, key='layer_utfvi_opacity')

    if show_lst:
        Map.addLayer(lst.clip(aoi), {
            'min': 0, 'max': 56,
            'palette': ['darkblue', 'blue', 'lightblue', 'green', 'yellow', 'orange', 'red'],
            'opacity': lst_opacity
        }, 'LST (°C)')

    if show_utfvi:
        Map.addLayer(utfvi.clip(aoi), {
            'min': -0.4, 'max': 0.4,
            'palette': ['blue', 'green', 'yellow', 'orange', 'red'],
            'opacity': utfvi_opacity
        }, 'UTFVI')

    Map.to_streamlit(width=700, height=500, scrolling=True, add_layer_control=True)

    with left_col.expander("Analysis Summary", expanded=True):
        st.write("### Mean NDVI: {:.2f}".format(ndvi_mean.getInfo()))
        st.write("### Mean LST: {:.2f} °C".format(lst_mean.getInfo()))
        st.write("### Mean UTFVI: {:.4f}".format(utfvi_mean.getInfo()))
        st.write("### Ecological Class: {}".format(classify_utfvi(utfvi_mean.getInfo())))
        st.write("(Higher UTFVI = more ecological stress)")

if not STREAMLIT_MODE:
    print("Mean NDVI: {:.2f}".format(ndvi_mean.getInfo()))
    print("Mean LST: {:.2f} °C".format(lst_mean.getInfo()))
    print("Mean UTFVI: {:.4f}".format(utfvi_mean.getInfo()))
    print("Ecological Class: {}".format(classify_utfvi(utfvi_mean.getInfo())))
    print("(Higher UTFVI = more ecological stress)")
    Map.add_child(folium.LayerControl())
    display(Map)

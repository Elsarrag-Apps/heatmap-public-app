import ee
import math
import geemap.foliumap as geemap
import folium
import tempfile
import requests
import streamlit as st
import streamlit.components.v1 as components
from urllib.parse import quote
from geopy.distance import geodesic


# -------------------------------
# PAGE SETUP
# -------------------------------
st.set_page_config(page_title="Climate Resilience Tool", layout="wide")

# Logos close together
logo_col1, logo_col2, _ = st.columns([0.12, 0.08, 0.80])
with logo_col1:
    st.image("hoarelea_logo.png", width=120)
with logo_col2:
    st.image("ukgbc_logo.png", width=70)

# Mode selector
mode = st.radio(
    "Select View Mode",
    ["Building Overheating Risk", "Urban Heat Risk"],
    key="mode_selector"
)

# Shared postcode input
postcode = st.text_input("Enter UK Postcode:", value="SW1A 1AA", key="shared_postcode")


# -------------------------------
# EARTH ENGINE AUTHENTICATION
# -------------------------------
try:
    with tempfile.NamedTemporaryFile(mode="w+", suffix=".json", delete=False) as f:
        f.write(st.secrets["earthengine"]["private_key"])
        key_path = f.name

    service_account = st.secrets["earthengine"]["service_account"]
    credentials = ee.ServiceAccountCredentials(service_account, key_path)
    ee.Initialize(credentials)
    st.success("✅ Earth Engine authenticated successfully.")
except Exception as e:
    st.error(f"Earth Engine authentication failed: {e}")
    st.stop()


# -------------------------------
# POSTCODE LOOKUP HELPER
# Replaces Nominatim to avoid rate limit errors
# -------------------------------
@st.cache_data(ttl=86400, show_spinner=False)
def lookup_uk_postcode(postcode):
    normalized = " ".join(postcode.strip().upper().split())
    if not normalized:
        return None

    try:
        response = requests.get(
            f"https://api.postcodes.io/postcodes/{quote(normalized)}",
            timeout=10
        )
        if response.status_code != 200:
            return None

        data = response.json().get("result")
        if not data:
            return None

        return {
            "postcode": data.get("postcode", normalized),
            "latitude": float(data["latitude"]),
            "longitude": float(data["longitude"])
        }
    except requests.RequestException:
        return None


# -------------------------------
# HELPER FUNCTIONS FOR RISK LOOKUP
# -------------------------------
def normalize_key(value):
    return (
        str(value)
        .strip()
        .lower()
        .replace("–", "-")
        .replace("—", "-")
        .replace("°c", "c")
        .replace("scenario", "")
        .replace(" ", "")
        .replace("-", "")
    )

def get_matching_key(mapping, target):
    if not isinstance(mapping, dict):
        return None

    if target in mapping:
        return target

    normalized_target = normalize_key(target)

    for key in mapping.keys():
        if normalize_key(key) == normalized_target:
            return key

    for key in mapping.keys():
        nk = normalize_key(key)
        if normalized_target in nk or nk in normalized_target:
            return key

    return None


# -------------------------------
# SHARED LAYOUT
# -------------------------------
left_col, right_col = st.columns([1, 2])
Map = geemap.Map(center=[51.5, -0.1], zoom=10, basemap='SATELLITE', ee_initialize=False)


# -------------------------------
# MODE 1: URBAN HEAT RISK
# -------------------------------
if mode == "Urban Heat Risk":

    with left_col:
        st.markdown("## 🏙️ Urban Heat Island Risk Tool")
        buffer_radius = st.slider("Buffer radius (meters)", 100, 2000, 500, key="urban_buf")
        selected_year = st.selectbox("Select Year", [str(y) for y in range(2013, 2026)], key="urban_year")
        date_range = st.selectbox(
            "Date Range",
            ['Spring-Summer-Autumn (Apr to Sep)', 'Summer (June to Aug)'],
            key="urban_daterange"
        )
        cloud_cover = st.slider("Cloud Cover Threshold (%)", 5, 50, 20, key="urban_cloud")
        run_analysis = st.button("Run Analysis", key="urban_run")

    if run_analysis:
        location = lookup_uk_postcode(postcode)
        if location is None:
            st.error(f"Invalid postcode: {postcode}")
            st.stop()

        lat, lon = location["latitude"], location["longitude"]
        point = ee.Geometry.Point([lon, lat])
        aoi = point.buffer(buffer_radius)

        start_date = f"{selected_year}-{'04-01' if date_range == 'Spring-Summer-Autumn (Apr to Sep)' else '06-01'}"
        end_date = f"{selected_year}-{'09-30' if date_range == 'Spring-Summer-Autumn (Apr to Sep)' else '08-31'}"

        def cloud_mask(image):
            qa = image.select('QA_PIXEL')
            mask = qa.bitwiseAnd(1 << 3).Or(qa.bitwiseAnd(1 << 5))
            return image.updateMask(mask.Not())

        collection = ee.ImageCollection("LANDSAT/LC08/C02/T1_TOA") \
            .filterDate(start_date, end_date) \
            .filterBounds(aoi) \
            .map(cloud_mask) \
            .filter(ee.Filter.lt('CLOUD_COVER', cloud_cover))

        image_count = collection.size().getInfo()
        if image_count == 0:
            st.warning("No Landsat scenes found for this location and selected settings.")
            st.stop()

        IC = collection.mean()

        ndvi = IC.normalizedDifference(['B5', 'B4']).rename('NDVI')
        ndvi_stats = ndvi.reduceRegion(
            ee.Reducer.minMax().combine('mean', '', True),
            geometry=aoi,
            scale=30,
            maxPixels=1e9
        )
        ndvi_mean = ee.Number(ndvi_stats.get('NDVI_mean'))
        ndvi_min = ee.Number(ndvi_stats.get('NDVI_min'))
        ndvi_max = ee.Number(ndvi_stats.get('NDVI_max'))

        thermal = IC.select('B10')
        fv = ndvi.subtract(ndvi_min).divide(ndvi_max.subtract(ndvi_min)).pow(2).rename('FV')
        em = fv.multiply(0.004).add(0.986).rename('EM')

        lst = thermal.expression(
            '(tb / (1 + (0.00115 * (tb / 0.48359547432)) * log(em))) - 273.15',
            {'tb': thermal.select('B10'), 'em': em}
        ).rename('LST')

        lst_mean = lst.reduceRegion(
            ee.Reducer.mean(),
            geometry=aoi,
            scale=30,
            maxPixels=1e9
        ).get('LST')

        utfvi = lst.subtract(ee.Image.constant(lst_mean)).divide(lst).rename('UTFVI')
        utfvi_mean = utfvi.reduceRegion(
            ee.Reducer.mean(),
            geometry=aoi,
            scale=30,
            maxPixels=1e9
        ).get('UTFVI')

        st.session_state.map_center = [lat, lon]
        st.session_state.map_postcode = location["postcode"]
        st.session_state.lst = lst.clip(aoi)
        st.session_state.utfvi = utfvi.clip(aoi)
        st.session_state.ndvi_mean = ndvi_mean.getInfo()
        st.session_state.lst_mean = ee.Number(lst_mean).getInfo()
        st.session_state.utfvi_mean = ee.Number(utfvi_mean).getInfo()
        st.session_state.image_count = image_count
        st.session_state.utfvi_class = (
            "Excellent" if st.session_state.utfvi_mean <= 0 else
            "Good" if st.session_state.utfvi_mean <= 0.005 else
            "Normal" if st.session_state.utfvi_mean <= 0.01 else
            "Bad" if st.session_state.utfvi_mean <= 0.015 else
            "Worse" if st.session_state.utfvi_mean <= 0.02 else
            "Worst"
        )

    with right_col:
        st.markdown("### Heat Map Viewer")
        Map = geemap.Map(center=[51.5, -0.1], zoom=10, basemap='SATELLITE', ee_initialize=False)

        if "map_center" in st.session_state:
            Map.set_center(st.session_state.map_center[1], st.session_state.map_center[0], 16)
            Map.add_child(folium.Marker(
                location=st.session_state.map_center,
                icon=folium.Icon(color='red', icon='tint', prefix='fa'),
                popup=f"Postcode: {st.session_state.get('map_postcode', postcode)}"
            ))

        show_lst = st.checkbox("Show Land Surface Temperature - LST", value=True, key="show_lst")
        lst_opacity = st.slider("LST Opacity", 0.0, 1.0, 0.6, key="lst_opacity")
        show_utfvi = st.checkbox("Show Urban Thermal Field Variance Index - UTFVI", value=True, key="show_utfvi")
        utfvi_opacity = st.slider("UTFVI Opacity", 0.0, 1.0, 0.6, key="utfvi_opacity")

        if "lst" in st.session_state and show_lst:
            Map.addLayer(st.session_state.lst, {
                'min': 0, 'max': 45,
                'palette': ['darkblue', 'blue', 'green', 'yellow', 'orange', 'red'],
                'opacity': lst_opacity
            }, 'LST')

        if "utfvi" in st.session_state and show_utfvi:
            Map.addLayer(st.session_state.utfvi, {
                'min': -0.005, 'max': 0.025,
                'palette': ['blue', 'green', 'yellow', 'orange', 'orangered', 'red'],
                'opacity': utfvi_opacity
            }, 'UTFVI')

        Map.to_streamlit(width=700, height=500, scrolling=True, add_layer_control=True)

        components.html("""
          <div style="display: flex; gap: 60px; font-family: Arial, sans-serif; margin-top: 20px;">
            <div>
              <h4 style="margin-bottom:5px">LST (°C)</h4>
              <div style="font-size:14px; line-height: 20px;">
                <div><span style="display:inline-block;width:15px;height:15px;background-color:darkblue;margin-right:6px;"></span> 0-7.5°C</div>
                <div><span style="display:inline-block;width:15px;height:15px;background-color:blue;margin-right:6px;"></span> 7.5–15°C</div>
                <div><span style="display:inline-block;width:15px;height:15px;background-color:green;margin-right:6px;"></span> 15–22.5°C</div>
                <div><span style="display:inline-block;width:15px;height:15px;background-color:yellow;margin-right:6px;"></span> 22.5–30°C</div>
                <div><span style="display:inline-block;width:15px;height:15px;background-color:orange;margin-right:6px;"></span> 30–37.5°C</div>
                <div><span style="display:inline-block;width:15px;height:15px;background-color:red;margin-right:6px;"></span> 37.5-45°C</div>
              </div>
            </div>

            <div>
              <h4 style="margin-bottom:5px">UTFVI (Ecological Evaluation)</h4>
              <div style="font-size:14px; line-height: 20px;">
                <div><span style="display:inline-block;width:15px;height:15px;background-color:blue;margin-right:6px;"></span> &lt;0 — Excellent</div>
                <div><span style="display:inline-block;width:15px;height:15px;background-color:green;margin-right:6px;"></span> 0–0.005 — Good</div>
                <div><span style="display:inline-block;width:15px;height:15px;background-color:yellow;margin-right:6px;"></span> 0.005–0.010 — Normal</div>
                <div><span style="display:inline-block;width:15px;height:15px;background-color:orange;margin-right:6px;"></span> 0.01–0.015 — Bad</div>
                <div><span style="display:inline-block;width:15px;height:15px;background-color:orangered;margin-right:6px;"></span> 0.015–0.02 — Worse</div>
                <div><span style="display:inline-block;width:15px;height:15px;background-color:red;margin-right:6px;"></span> &gt; 0.020 — Worst</div>
              </div>
            </div>
          </div>
        """, height=240)

    with left_col.expander("Analysis Summary", expanded=True):
        if "ndvi_mean" in st.session_state:
            st.write(f"### Mean LST: {st.session_state.lst_mean:.2f} °C")
            st.write(f"### Mean UTFVI: {st.session_state.utfvi_mean:.4f}")
            st.write(f"### UTFVI Class: {st.session_state.utfvi_class}")
            st.write(f"### Scenes used: {st.session_state.image_count}")


# -------------------------------
# MODE 2: BUILDING OVERHEATING RISK
# -------------------------------
elif mode == "Building Overheating Risk":
    from risk_data_office import risk_data_office
    from risk_data_highrise import risk_data_highrise
    from risk_data_lowrise import risk_data_lowrise
    from risk_data_school import risk_data_school
    from risk_data_carehome import risk_data_carehome
    from risk_data_healthcare import risk_data_healthcare

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

        city_coords = {
            "Leeds": (53.8008, -1.5491),
            "Nottingham": (52.9548, -1.1581),
            "London": (51.5074, -0.1278),
            "Glasgow": (55.8642, -4.2518),
            "Cardiff": (51.4816, -3.1791),
            "Swindon": (51.5558, -1.7797)
        }

        with left_col:
            st.markdown("## 🏢 Building Overheating Risk Tool")
            postcode_b = postcode

            building_type = st.selectbox(
                "Building Type",
                ["Low-Rise Residential", "High-Rise Residential", "Office", "School", "Care Home", "Healthcare"],
                key="btype"
            )

            age_band = st.selectbox(
                "Age Band",
                ["Pre-1945", "1945–1970", "1970–2000", "2000–2020", "New Build"],
                key="ageband"
            )

            mitigation_full = st.radio(
                "Select Mitigation Strategy",
                [
                    "Baseline – No overheating adaptation measures",
                    "Passive – Shading, natural ventilation, thermal mass, night purge, solar control",
                    "Active – MVHR, fans, automated shading systems"
                ],
                key="mitigation_detailed"
            )

            mitigation = mitigation_full.split("–")[0].strip()

            climate = st.selectbox(
                "Climate Scenario",
                ["2°C", "3°C", "4°C"],
                key="climate",
                help="""
                - **2°C**: Low global warming scenario
                - **3°C**: Medium global warming scenario
                - **4°C**: High global warming scenario
                """
            )

            st.markdown("""
            <small>
            <strong>2°C:</strong> Low global warming scenario<br>
            <strong>3°C:</strong> Medium global warming scenario<br>
            <strong>4°C:</strong> High global warming scenario
            </small>
            """, unsafe_allow_html=True)

        # -------------------------------
        # POSTCODE LOOKUP
        # -------------------------------
        location_b = lookup_uk_postcode(postcode_b)
        if location_b is None:
            with left_col:
                st.error(f"Invalid postcode: {postcode_b}")
            with right_col:
                st.markdown("### Risk Map")
                Map = geemap.Map(center=[51.5, -0.1], zoom=6, basemap='SATELLITE', ee_initialize=False)
                Map.to_streamlit(width=700, height=500, scrolling=True, add_layer_control=True)
            return

        lat_b, lon_b = location_b["latitude"], location_b["longitude"]
        postcode_coords = (lat_b, lon_b)

        matched_city, distance_km = min(
            ((city, geodesic(postcode_coords, coords).km) for city, coords in city_coords.items()),
            key=lambda x: x[1]
        )

        with left_col:
            st.success(f"📌 Nearest city: {matched_city} ({distance_km:.1f} km)")

        # -------------------------------
        # MERGE RISK DATA
        # -------------------------------
        risk_data = {}
        for city in ["London", "Leeds", "Nottingham", "Glasgow", "Cardiff", "Swindon"]:
            risk_data[city] = {
                **risk_data_office.get(city, {}),
                **risk_data_lowrise.get(city, {}),
                **risk_data_highrise.get(city, {}),
                **risk_data_school.get(city, {}),
                **risk_data_carehome.get(city, {}),
                **risk_data_healthcare.get(city, {}),
            }

        # -------------------------------
        # ROBUST LOOKUP
        # -------------------------------
        city_data = risk_data.get(matched_city, {})
        building_key = get_matching_key(city_data, building_type)
        building_data = city_data.get(building_key, {}) if building_key else {}

        age_key = get_matching_key(building_data, age_band)
        age_data = building_data.get(age_key, {}) if age_key else {}

        mitigation_key = get_matching_key(age_data, mitigation)
        mitigation_data = age_data.get(mitigation_key, {}) if mitigation_key else {}

        climate_key = get_matching_key(mitigation_data, climate)
        entry = mitigation_data.get(climate_key) if climate_key else None

        # -------------------------------
        # RIGHT PANEL / MAP OUTPUT
        # -------------------------------
        with right_col:
            st.markdown("### Risk Map")

            # Tight postcode zoom so the overheating circle is visible
            Map = geemap.Map(basemap='SATELLITE', ee_initialize=False)
            Map.set_center(lon_b, lat_b, 18)

            lat_pad = 0.0018
            lon_pad = lat_pad / max(math.cos(math.radians(lat_b)), 0.1)

            Map.fit_bounds([
                [lat_b - lat_pad, lon_b - lon_pad],
                [lat_b + lat_pad, lon_b + lon_pad]
            ])

            Map.add_child(folium.Marker(
                location=[lat_b, lon_b],
                popup=f"Postcode: {location_b['postcode']}"
            ))

            if entry:
                level = int(entry["level"])
                label = risk_categories[level]["label"]
                scenario = entry.get("scenario", climate)
                color = risk_categories[level]["color"]

                with st.expander("ℹ️ What do the Risk Levels mean?"):
                    if building_type in ["Low-Rise Residential", "High-Rise Residential"]:
                        st.markdown("""
                        - **1 – Low**: 0–3 overheating days; meets TM52/TM59 thresholds  
                        - **2 – Medium**: 3–6 days above 28°C or adaptive limit  
                        - **3 – High**: 6–9 days; discomfort likely  
                        - **4 – Very High**: 9–12 days >28°C  
                        - **5 – Extreme**: >12 days; health risk likely
                        """)
                    elif building_type == "Office":
                        st.markdown("""
                        - **1 – Low**: Meets summer comfort criteria TM52  
                        - **2 – Medium**: 3–6 days >28°C; minor discomfort  
                        - **3 – High**: >6 days >28°C; productivity may decline  
                        - **4 – Very High**: Frequent discomfort, 9+ days affected  
                        - **5 – Extreme**: Over 12 days; critical indoor temps
                        """)
                    elif building_type == "School":
                        st.markdown("""
                        - **1 – Low**: Within TM52/BB101 comfort standards  
                        - **2 – Medium**: up to 4 days; some discomfort during teaching hours  
                        - **3 – High**: 4–7 overheating days; learning disrupted  
                        - **4 – Very High**: 7–9 days above 28°C  
                        - **5 – Extreme**: 9+ days; unsafe learning conditions
                        """)
                    elif building_type in ["Care Home", "Healthcare"]:
                        st.markdown("""
                        - **1 – Low**: Safe and thermally comfortable  
                        - **2 – Medium**: 3–4 days; minor discomfort for occupants  
                        - **3 – High**: 4–5 days above limits; health concern  
                        - **4 – Very High**: 5–6 days; disruption to operations or patient comfort  
                        - **5 – Extreme**: 6+ days; serious health risk
                        """)

                st.markdown(f"""
                <div style='display:flex;align-items:center;gap:8px;margin-bottom:8px;'>
                  <div style='width:14px;height:14px;border-radius:50%;background-color:{color};'></div>
                  <div style='font-size:20px;font-weight:bold;'>
                    Risk Level {level} – {label}
                  </div>
                </div>
                <div style='font-size:18px;font-weight:normal;margin-left:22px;'>
                  {scenario}
                </div>
                <div style='font-size:14px;margin-left:22px;color:#666;'>
                  {matched_city} | {building_type} | {age_band} | {mitigation}
                </div>
                """, unsafe_allow_html=True)

                Map.add_child(folium.Circle(
                    location=[lat_b, lon_b],
                    radius=50,
                    color=color,
                    weight=4,
                    fill=True,
                    fill_color=color,
                    fill_opacity=0.4,
                    popup=f"Risk Level {level} – {label}"
                ))

                Map.to_streamlit(width=700, height=500, scrolling=True, add_layer_control=True)
                st.markdown(risk_legend_html, unsafe_allow_html=True)

            else:
                st.warning("❌ No risk data found for this selection.")

                Map.add_child(folium.Circle(
                    location=[lat_b, lon_b],
                    radius=50,
                    color="blue",
                    weight=4,
                    fill=True,
                    fill_color="blue",
                    fill_opacity=0.2,
                    popup="Postcode location"
                ))

                Map.to_streamlit(width=700, height=500, scrolling=True, add_layer_control=True)

                with st.expander("Debug lookup"):
                    st.write("Matched city:", matched_city)
                    st.write("Selected building type:", building_type)
                    st.write("Selected age band:", age_band)
                    st.write("Selected mitigation:", mitigation)
                    st.write("Selected climate:", climate)
                    st.write("Available building types:", list(city_data.keys()))
                    if building_data:
                        st.write("Available age bands:", list(building_data.keys()))
                    if age_data:
                        st.write("Available mitigation keys:", list(age_data.keys()))
                    if mitigation_data:
                        st.write("Available climate keys:", list(mitigation_data.keys()))

    run_building_overheating_risk(left_col, right_col, Map)










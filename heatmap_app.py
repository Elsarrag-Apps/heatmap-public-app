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
st.set_page_config(page_title="Climate Resilience Tool", layout="wide")

col1, col2 = st.columns([1, 1])
with col1:
    st.image("hoarelea_logo.png", width=120)
with col1:
    st.image("ukgbc_logo.png", width=70)

# Mode selector
mode = st.radio("Select View Mode", ["Building Overheating Risk", "Urban Heat Risk"], key="mode_selector")

# ✅ Shared postcode input for both modes
postcode = st.text_input("Enter UK Postcode:", value="SW1A 1AA", key="shared_postcode")

# ✅ Earth Engine authentication (fixed)
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
Map = geemap.Map(center=[51.5, -0.1], zoom=10, basemap='SATELLITE', ee_initialize=False)

# -------------------------------
# MODE 1: Urban Heat Risk
# -------------------------------
if mode == "Urban Heat Risk":
    with left_col:
        st.markdown("## 🏙️ Urban Heat Island Risk Tool")
        buffer_radius = st.slider("Buffer radius (meters)", 100, 2000, 500, key="urban_buf")
        selected_year = st.selectbox("Select Year", [str(y) for y in range(2013, 2025)], key="urban_year")
        date_range = st.selectbox("Date Range", ['Spring-Summer-Autumn (Apr to Sep)', 'Summer (June to Aug)'], key="urban_daterange")
        cloud_cover = st.slider("Cloud Cover Threshold (%)", 5, 50, 20, key="urban_cloud")
        run_analysis = st.button("Run Analysis", key="urban_run")

    if run_analysis:
        location = geocode_with_retry(postcode)
        if location is None:
            st.error(f"Invalid postcode: {postcode}")
            st.stop()

        lat, lon = location.latitude, location.longitude
        point = ee.Geometry.Point([lon, lat])
        aoi = point.buffer(buffer_radius)

        start_date = f"{selected_year}-{'04-01' if date_range == 'Spring-Summer-Autumn (Apr to Sep)' else '06-01'}"
        end_date = f"{selected_year}-{'09-30' if date_range == 'Spring-Summer-Autumn (Apr to Sep)' else '08-31'}"

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
        ndvi_stats = ndvi.reduceRegion(ee.Reducer.minMax().combine('mean', '', True), geometry=aoi, scale=30, maxPixels=1e9)
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
        lst_mean = lst.reduceRegion(ee.Reducer.mean(), geometry=aoi, scale=30, maxPixels=1e9).get('LST')

        utfvi = lst.subtract(ee.Image.constant(lst_mean)).divide(lst).rename('UTFVI')
        utfvi_mean = utfvi.reduceRegion(ee.Reducer.mean(), geometry=aoi, scale=30, maxPixels=1e9).get('UTFVI')

        st.session_state.map_center = [lat, lon]
        st.session_state.lst = lst.clip(aoi)
        st.session_state.utfvi = utfvi.clip(aoi)
        st.session_state.ndvi_mean = ndvi_mean.getInfo()
        st.session_state.lst_mean = lst_mean.getInfo()
        st.session_state.utfvi_mean = utfvi_mean.getInfo()
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
                popup=f"Postcode: {postcode}"
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

        import streamlit.components.v1 as components
        components.html("""
          <div style="display: flex; gap: 60px; font-family: Arial, sans-serif; margin-top: 20px;">
            <!-- LST Legend -->
            <div>
              <h4 style="margin-bottom:5px">LST (°C)</h4>
              <div style="font-size:14px; line-height: 20px;">
                <div><span style="display:inline-block;width:15px;height:15px;background-color:darkblue;margin-right:6px;"></span>  0-7.5°C</div>
                <div><span style="display:inline-block;width:15px;height:15px;background-color:blue;margin-right:6px;"></span> 7.5–15°C</div>
                <div><span style="display:inline-block;width:15px;height:15px;background-color:green;margin-right:6px;"></span> 15–22.5°C</div>
                <div><span style="display:inline-block;width:15px;height:15px;background-color:yellow;margin-right:6px;"></span> 22.5–30°C</div>
                <div><span style="display:inline-block;width:15px;height:15px;background-color:orange;margin-right:6px;"></span> 30–37.5°C</div>
                <div><span style="display:inline-block;width:15px;height:15px;background-color:red;margin-right:6px;"></span> 37.5-45°C</div>
              </div>
            </div>
            <!-- UTFVI Legend -->
            <div>
              <h4 style="margin-bottom:5px">UTFVI (Ecological Evaluation)</h4>
              <div style="font-size:14px; line-height: 20px;">
                <div><span style="display:inline-block;width:15px;height:15px;background-color:blue;margin-right:6px;"></span> <0 — Excellent</div>
                <div><span style="display:inline-block;width:15px;height:15px;background-color:green;margin-right:6px;"></span> 0–0.005 — Good</div>
                <div><span style="display:inline-block;width:15px;height:15px;background-color:yellow;margin-right:6px;"></span> 0.005–0.010 — Normal</div>
                <div><span style="display:inline-block;width:15px;height:15px;background-color:orange;margin-right:6px;"></span> 0.01–0.015 — Bad</div>
                <div><span style="display:inline-block;width:15px;height:15px;background-color:orangered;margin-right:6px;"></span> 0.015–0.02 — Worse</div>
                <div><span style="display:inline-block;width:15px;height:15px;background-color:red;margin-right:6px;"></span> > 0.020 — Worst</div>
              </div>
            </div>
          </div>
        """, height=240)

    with left_col.expander("Analysis Summary", expanded=True):
        if "ndvi_mean" in st.session_state:
            st.write(f"### Mean LST: {st.session_state.lst_mean:.2f} °C")
            st.write(f"### Mean UTFVI: {st.session_state.utfvi_mean:.4f}")

# -------------------------------
# MODE 2: Building Overheating Risk
# -------------------------------
elif mode == "Building Overheating Risk":
    from risk_data_office import risk_data_office
    from risk_data_highrise import risk_data_highrise
    from risk_data_lowrise import risk_data_lowrise
    from risk_data_school import risk_data_school
    from risk_data_carehome import risk_data_carehome
    from risk_data_healthcare import risk_data_healthcare
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
            postcode_b = postcode
            building_type = st.selectbox("Building Type", ["Low-Rise Residential", "High-Rise Residential", "Office","School", "Care Home", "Healthcare"], key="btype")

            age_band = st.selectbox("Age Band", ["Pre-1945", "1945–1970", "1970–2000", "2000–2020", "New Build"], key="ageband")
            mitigation_full = st.radio(
                "Select Mitigation Strategy",
                [
                  "Baseline – No overheating adaptation measures",
                  "Passive – Shading, natural ventilation, thermal mass, night purge, solar control",
                  "Active – MVHR, fans, automated shading systems"
               ],
          key="mitigation_detailed"
          )

     # ✅ Clean value used for risk_data lookup
            mitigation = mitigation_full.split(" – ")[0]


             
           # mitigation = st.radio("Mitigation", ["Baseline", "Passive", "Active"], 
                # key="mitigation",
              #   help="""
            #     • Baseline: Standard build with no overheating adaptation measures.
             #    • Passive: Includes shading, natural ventilation, thermal mass, night purge, and solar control glazing.
             #    • Active: Includes MVHR (Mechanical Ventilation with Heat Recovery), fans, and automated shading systems.
                #   """
           # )

             
       #     climate = st.selectbox("Climate Scenario", ["2°C", "3°C", "4°C"], key="climate")
                     
                   
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
           
            risk_data = {}

# Merge each building type under city keys
            for city in ["London", "Leeds", "Nottingham", "Glasgow", "Cardiff", "Swindon"]:
                risk_data[city] = {
                    **risk_data_office.get(city, {}),
                    **risk_data_lowrise.get(city, {}),
                    **risk_data_highrise.get(city, {}),
                    **risk_data_school.get(city, {}),
                    **risk_data_carehome.get(city, {}),
                    **risk_data_healthcare.get(city, {}),
                }

          
            
            st.write("🔍 Lookup path:", matched_city, building_type, age_band, mitigation, climate)

            entry = risk_data.get(matched_city, {}).get(building_type, {}).get(age_band, {}).get(mitigation, {}).get(climate)
            if entry:
                level = entry["level"]
                label = risk_categories[level]["label"]
                scenario = entry["scenario"]
                color = risk_categories[level]["color"]

                

                circle = ee.Geometry.Point([lon_b, lat_b]).buffer(100)
                Map.set_center(lon_b, lat_b, 18)
                Map.addLayer(circle, {"color": color}, "Risk Circle")
            
            if entry is None:
                st.warning("❌ No risk data found for this selection.")

                    # 🔍 Optional debug output
                city_data = risk_data.get(matched_city)
                if city_data:
                    type_data = city_data.get(building_type)
                    if not type_data:
                        st.info(f"❓ Building type '{building_type}' not found in {matched_city}")
                        st.write("✅ Available types:", list(city_data.keys()))
                    elif age_band not in type_data:
                        st.info(f"❓ Age band '{age_band}' not found for {building_type}")
                        st.write("✅ Available age bands:", list(type_data.keys()))
                    elif mitigation not in type_data[age_band]:
                        st.info(f"❓ Mitigation '{mitigation}' not found")
                        st.write("✅ Available strategies:", list(type_data[age_band].keys()))
                    elif climate not in type_data[age_band][mitigation]:
                        st.info(f"❓ Climate '{climate}' not found")
                        st.write("✅ Available climates:", list(type_data[age_band][mitigation].keys()))
                else:
                    st.error(f"🚫 No data for city: {matched_city}")

            if entry:
                level = entry["level"]
                label = risk_categories[level]["label"]
                scenario = entry["scenario"]
                color = risk_categories[level]["color"]

                with right_col:
                    st.markdown("### Risk Map")
                    with st.expander("ℹ️ What do the Risk Levels mean?"):
                        if building_type == "Low-Rise Residential" or building_type == "High-Rise Residential":
                            st.markdown("""
                            - **1 – Low**: 0–3 overheating days; meets TM59/TM52 thresholds  
                            - **2 – Medium**: 3–6 days above 28°C or adaptive limit  
                            - **3 – High**: 6–9 days; discomfort likely  
                            - **4 – Very High**: 9–12 days or 4+ nights >27°C  
                            - **5 – Extreme**: >12 days; health risk likely
                            """)
                        elif building_type == "Office":
                            st.markdown("""
                            - **1 – Low**: Meets summer comfort criteria (Guide A)  
                            - **2 – Medium**: Some hours >25°C; minor discomfort  
                            - **3 – High**: >6 days >28°C; productivity may decline  
                            - **4 – Very High**: Frequent discomfort, 9+ days affected  
                            - **5 – Extreme**: Over 12 days; critical indoor temps
                            """)
                        elif building_type == "School":
                            st.markdown("""
                            - **1 – Low**: Within BB101 comfort standards  
                            - **2 – Medium**: Some discomfort during teaching hours  
                            - **3 – High**: 6+ overheating days; learning disrupted  
                            - **4 – Very High**: >9 days above 28°C  
                            - **5 – Extreme**: 12+ days; unsafe learning conditions
                            """)
                        elif building_type == "Care Home" or building_type == "Healthcare":
                            st.markdown("""
                            - **1 – Low**: Safe and thermally comfortable  
                            - **2 – Medium**: Minor discomfort for occupants  
                            - **3 – High**: 6+ days above limits; health concern  
                            - **4 – Very High**: Disruption to operations or patient comfort  
                            - **5 – Extreme**: 12+ days; serious health risk
                            """)  
                      
                    
                   
                     

                    st.markdown(f"""
                    <div style='font-size:20px; font-weight:bold; margin-bottom:10px;'>
                    🛑 Risk Level {level} – {label}<br>
                    <span style='font-size:18px; font-weight:normal;'>{scenario}</span>
                    </div>
                    """, unsafe_allow_html=True)

                    Map.set_center(lon_b, lat_b, 18)
                    circle = ee.Geometry.Point([lon_b, lat_b]).buffer(100)
                    Map.addLayer(circle, {"color": color}, "Risk Circle")

                    Map.to_streamlit(width=700, height=500, scrolling=True, add_layer_control=True)
                    st.markdown(risk_legend_html, unsafe_allow_html=True)



    run_building_overheating_risk(left_col, right_col, Map)








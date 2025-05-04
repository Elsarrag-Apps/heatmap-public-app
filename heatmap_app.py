
import ee
import geemap.foliumap as geemap
import folium
import json
import tempfile
import streamlit as st
from geopy.geocoders import Nominatim
from geopy.exc import GeocoderTimedOut

st.set_page_config(page_title="Urban Heat Risk Viewer", layout="wide")
mode = st.radio("Select View Mode", ["Urban Heat Risk", "Building Overheating Risk"])  # <– THIS MUST BE HERE

import streamlit as st

st.set_page_config(layout="wide")

st.title("TEST MODE SWITCH")

mode = st.radio("Select View Mode", ["Urban Heat Risk", "Building Overheating Risk"])
st.write(f"You selected: {mode}")







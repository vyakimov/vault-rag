import streamlit as st

retrieve_page = st.Page("streamlit_search.py", title="Retrieve", icon="🔍")
synthesize_page = st.Page("streamlit_llm.py", title="Synthesize", icon="🤖")
notes_page = st.Page("streamlit_db.py", title="Notes", icon="🗂️")

navigation = st.navigation([retrieve_page, synthesize_page, notes_page])
navigation.run()

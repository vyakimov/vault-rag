import streamlit as st

search_page = st.Page("streamlit_search.py", title="Search", icon="🔍")
answer_page = st.Page("streamlit_llm.py", title="Answer", icon="🤖")
notes_page = st.Page("streamlit_db.py", title="Notes", icon="🗂️")

navigation = st.navigation([search_page, answer_page, notes_page])
navigation.run()

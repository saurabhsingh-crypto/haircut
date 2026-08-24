# Superseded FastAPI build

`haircut_app.py` and `index.html` are the previous single-file FastAPI +
Alpine/Plotly build. They are kept only for reference while the Streamlit app
is being verified.

Do not deploy them. They carry the findings from the August 2026 hosting
audit that the Streamlit build fixes: no authentication on any endpoint,
in-memory sessions that break under more than one worker, no database TLS or
connect timeout, and unbounded uploads.

The business logic they contained now lives in `haircut_core/`, which has no
web framework dependency. Once you are happy with the Streamlit app, delete
this folder.

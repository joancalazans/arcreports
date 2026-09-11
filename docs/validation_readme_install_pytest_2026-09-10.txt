....................................s................................... [ 21%]
........................................................................ [ 43%]
........................................................................ [ 65%]
........................................................................ [ 87%]
........................................                                 [100%]
=============================== warnings summary ===============================
app/main.py:993
  /opt/sites/arcreports/app/main.py:993: DeprecationWarning:
          on_event is deprecated, use lifespan event handlers instead.
  
          Read more about it in the
          [FastAPI docs for Lifespan Events](https://fastapi.tiangolo.com/advanced/events/).
          
    @app.on_event("startup")

venv/lib64/python3.9/site-packages/fastapi/applications.py:4579
venv/lib64/python3.9/site-packages/fastapi/applications.py:4579
  /opt/sites/arcreports/venv/lib64/python3.9/site-packages/fastapi/applications.py:4579: DeprecationWarning:
          on_event is deprecated, use lifespan event handlers instead.
  
          Read more about it in the
          [FastAPI docs for Lifespan Events](https://fastapi.tiangolo.com/advanced/events/).
          
    return self.router.on_event(event_type)

app/main.py:1071
  /opt/sites/arcreports/app/main.py:1071: DeprecationWarning:
          on_event is deprecated, use lifespan event handlers instead.
  
          Read more about it in the
          [FastAPI docs for Lifespan Events](https://fastapi.tiangolo.com/advanced/events/).
          
    @app.on_event("shutdown")

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
327 passed, 1 skipped, 4 warnings in 12.44s

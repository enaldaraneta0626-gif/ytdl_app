web: python -c "from app import app, db; app.app_context().__enter__(); db.create_all()" && gunicorn app:app --workers 2 --timeout 120

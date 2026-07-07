"""Presentation/service layer shared by the Jinja UI and the JSON API.

Nothing here talks to FastAPI or a template - it turns stored engine output into
the numbers both front-ends show, so the admin dashboard and the product API can
never drift apart.
"""

# BlockingBear NGINX image: the installation's single HTTP entry point. It
# serves the SPA and proxies /api to the backend.
#
# Build (context is this directory):
#     docker build -t blockingbear-nginx:0.1.0 nginx
#
# The SPA arrives through the `spa` volume after the frontend one-shot exits.
# This keeps the frontend build in frontend/ and all web configuration here.
#
# The frontend uses relative API URLs, so SPA and API need the same origin or a
# reverse proxy. This image provides that proxy and serves the SPA.

FROM nginx:1.27-alpine

# The official entrypoint renders templates through envsubst into conf.d.
# NGINX_ENVSUBST_FILTER restricts replacement to BACKEND_ORIGIN; otherwise
# envsubst would also consume nginx variables such as $uri and $host.
ENV BACKEND_ORIGIN=http://backend:8000 \
    NGINX_ENVSUBST_FILTER=BACKEND_ORIGIN

COPY nginx.conf /etc/nginx/nginx.conf
COPY templates/ /etc/nginx/templates/

EXPOSE 80

# Probing the root checks both nginx and SPA publication. BusyBox wget is already
# available in the Alpine image.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD wget -q --spider http://127.0.0.1/ || exit 1

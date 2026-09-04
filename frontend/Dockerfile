# BlockingBear FRONTEND image: build and publish the SPA.
#
# Build (context is this directory):
#     docker build -t blockingbear-frontend:0.1.0 frontend
#
# This is not a server. It is a one-shot service that copies the build output to
# the `spa` volume and exits. nginx serves that read-only volume.
#
# The separation keeps all web-server configuration in nginx/. Compose orders it
# with
#     depends_on: frontend: condition: service_completed_successfully
# so nginx starts only after publication succeeds.

# Node 22 satisfies the engines declared by Vite and its React plugin. Alpine is
# supported because package-lock.json contains the musl and glibc variants of
# @rolldown/binding, @tailwindcss/oxide, and lightningcss.
FROM node:22-alpine

WORKDIR /build

# `npm ci` installs the versioned lockfile exactly. Copy manifests first so the
# dependency layer remains cached until dependencies change.
COPY package.json package-lock.json ./
RUN npm ci

COPY . .

# The build needs no environment variables; backend routing is an nginx runtime
# choice rather than a compile-time frontend value.
RUN npm run build

# Clear the volume before copying because hashed assets are cached for one year;
# otherwise obsolete builds would accumulate indefinitely.
CMD ["sh", "-c", "rm -rf /spa/* /spa/.[!.]* 2>/dev/null; cp -a /build/dist/. /spa/ && echo \"SPA published to /spa: $(find /spa -type f | wc -l) files\""]

# ─── Build stage ─────────────────────────────────────────────
# Vite bakes VITE_* at build time. These are the *defaults* the workspace
# setup screen pre-fills; they must be URLs the BROWSER can reach (the SPA
# calls the services directly), so never use Docker service names here.
# Override per-deployment via compose build args. End users can also change
# them at runtime in the console's Workspace Settings (persisted client-side).
FROM node:20-alpine AS build

ARG VITE_INGESTION_URL=http://localhost:8080
ARG VITE_CONFIG_URL=http://localhost:8081
ARG VITE_QUERY_URL=http://localhost:8082
ARG VITE_AGENTS_URL=http://localhost:8083
ENV VITE_INGESTION_URL=$VITE_INGESTION_URL \
    VITE_CONFIG_URL=$VITE_CONFIG_URL \
    VITE_QUERY_URL=$VITE_QUERY_URL \
    VITE_AGENTS_URL=$VITE_AGENTS_URL

WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
# Produce the deploy bundle only — typechecking is CI's job (make lint-admin),
# not the image build's, so we skip the `tsc --noEmit` gate in `npm run build`.
RUN npx vite build

# ─── Serve stage ─────────────────────────────────────────────
FROM nginx:1.27-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80

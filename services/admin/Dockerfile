# ─── Build stage ─────────────────────────────────────────────
# The bundle contains no service URLs or credentials. Every API call stays
# same-origin and nginx forwards it to the authenticated admin backend.
FROM node:20-alpine@sha256:fb4cd12c85ee03686f6af5362a0b0d56d50c58a04632e6c0fb8363f609372293 AS build

WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
# Produce the deploy bundle only — typechecking is CI's job (make lint-admin),
# not the image build's, so we skip the `tsc --noEmit` gate in `npm run build`.
RUN npx vite build

# ─── Serve stage ─────────────────────────────────────────────
FROM nginx:1.27-alpine@sha256:65645c7bb6a0661892a8b03b89d0743208a18dd2f3f17a54ef4b76fb8e2f2a10
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80

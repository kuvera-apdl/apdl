# ─── Build stage ─────────────────────────────────────────────
# The bundle contains no service URLs or credentials. Every API call stays
# same-origin and nginx forwards it to the authenticated admin backend.
FROM node:26-alpine@sha256:e88a35be04478413b7c71c455cd9865de9b9360e1f43456be5951032d7ac1a66 AS build

WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
# Produce the deploy bundle only — typechecking is CI's job (make lint-admin),
# not the image build's, so we skip the `tsc --noEmit` gate in `npm run build`.
RUN npx vite build

# ─── Serve stage ─────────────────────────────────────────────
FROM nginx:1.31-alpine@sha256:7068961d45b07b2af510ac002e9daa63a1d3eba2111202d6768798690800fffd
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80

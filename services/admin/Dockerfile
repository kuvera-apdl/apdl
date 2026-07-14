# ─── Build stage ─────────────────────────────────────────────
# The bundle contains no service URLs or credentials. Every API call stays
# same-origin and nginx forwards it to the authenticated admin backend.
FROM node:20-alpine AS build

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

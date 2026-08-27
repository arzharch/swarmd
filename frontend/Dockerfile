# Next.js standalone build. Two stages so the runtime image carries the traced
# server and nothing else -- no npm, no source, no dev dependencies to patch.
FROM node:22-alpine AS deps
WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm ci --omit=dev --ignore-scripts

FROM node:22-alpine AS builder
WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm ci --ignore-scripts
COPY . .
ENV NEXT_TELEMETRY_DISABLED=1
RUN npm run build

FROM node:22-alpine AS runtime
WORKDIR /app
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3001

# Numeric uid so the Kubernetes runAsNonRoot check can verify it without
# resolving a name; with a username the kubelet cannot tell the user is
# non-root and admission fails at runtime rather than at deploy time.
RUN addgroup -g 10001 -S swarmd && adduser -u 10001 -S swarmd -G swarmd

COPY --from=builder --chown=10001:10001 /app/.next/standalone ./
COPY --from=builder --chown=10001:10001 /app/.next/static ./.next/static

USER 10001:10001
EXPOSE 3001

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s \
  CMD node -e "fetch('http://127.0.0.1:3001/api/health').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"

CMD ["node", "server.js"]

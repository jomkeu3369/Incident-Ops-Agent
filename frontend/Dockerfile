FROM node:20-alpine AS deps
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci

FROM node:20-alpine AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .
ARG NEXT_PUBLIC_GATEWAY_URL=http://localhost
ARG NEXT_PUBLIC_AG_UI_PATH=/api/ag-ui
ARG NEXT_PUBLIC_INCIDENT_API_PATH=/api/incidents
ENV NEXT_PUBLIC_GATEWAY_URL=${NEXT_PUBLIC_GATEWAY_URL}
ENV NEXT_PUBLIC_AG_UI_URL=${NEXT_PUBLIC_AG_UI_PATH}
ENV NEXT_PUBLIC_AG_UI_PATH=${NEXT_PUBLIC_AG_UI_PATH}
ENV NEXT_PUBLIC_INCIDENT_API_PATH=${NEXT_PUBLIC_INCIDENT_API_PATH}
ENV NEXT_TELEMETRY_DISABLED=1
RUN npm run build

FROM node:20-alpine AS runner
WORKDIR /app
ENV NODE_ENV=production
ENV NEXT_TELEMETRY_DISABLED=1
ENV HOSTNAME=0.0.0.0
ENV PORT=3000

RUN addgroup --system --gid 1001 nodejs && adduser --system --uid 1001 nextjs

COPY --from=builder --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /app/.next/static ./.next/static

USER nextjs
EXPOSE 3000

HEALTHCHECK --interval=15s --timeout=5s --start-period=15s --retries=5 \
  CMD wget -q --spider http://127.0.0.1:3000 || exit 1

CMD ["node", "server.js"]

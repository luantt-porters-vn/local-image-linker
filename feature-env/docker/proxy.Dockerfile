# syntax=docker/dockerfile:1
# Compile the proxy snapshot with Node tooling kept outside the final runtime stage.
ARG RUNTIME_IMAGE
FROM 710983083415.dkr.ecr.ap-northeast-1.amazonaws.com/base/node:AZ2023-N22.22.0 AS build
ENV npm_config_fetch_retries=3 npm_config_fetch_timeout=300000 npm_config_maxsockets=5
ENV npm_config_registry=http://registry.ps.porters.local:8081/repository/npm-group/
WORKDIR /src
# Install from the lockfile; credentials are temporary and downloads are cached.
COPY proxy/package*.json ./
RUN --mount=type=secret,id=npmrc,target=/root/.npmrc --mount=type=cache,target=/root/.npm npm ci --ignore-scripts
COPY proxy/ ./
RUN npm run build && npm prune --omit=dev --ignore-scripts
# Preserve runtime startup behavior but replace old code and production dependencies.
FROM ${RUNTIME_IMAGE}
RUN rm -rf /home/node/app/dist /home/node/app/node_modules
COPY --from=build --chown=node:node /src/dist/ /home/node/app/dist/
COPY --from=build --chown=node:node /src/node_modules/ /home/node/app/node_modules/
COPY --from=build --chown=node:node /src/package.json /home/node/app/package.json
# The proxy status endpoint signals readiness with HTTP 204.
HEALTHCHECK --interval=10s --timeout=5s --start-period=30s --retries=18 CMD node -e "require('http').get('http://127.0.0.1:3000/privateapi/status',r=>process.exit(r.statusCode===204?0:1)).on('error',()=>process.exit(1))"
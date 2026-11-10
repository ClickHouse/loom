{{- define "loom.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "loom.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/* `<fullname>-<suffix>` within `max` characters (63; a StatefulSet's 52, as
its pods get a `<name>-<hash>` label): the fullname is cut first, so a long
release still gets valid Job, StatefulSet and label names. */}}
{{- define "loom.suffixed" -}}
{{- $full := include "loom.fullname" .root -}}
{{- $max := int (.max | default 63) -}}
{{- printf "%s-%s" ($full | trunc (int (sub (sub $max 1) (len .suffix))) | trimSuffix "-") .suffix -}}
{{- end }}

{{- define "loom.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
{{ include "loom.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "loom.selectorLabels" -}}
app.kubernetes.io/name: {{ include "loom.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/* The extractor's pods carry a name of their own, never the selector labels
above: every policy, Service and Deployment that selects Loom's pods would take
them in too, and the web pods' NetworkPolicy would open their egress. */}}
{{- define "loom.extractorName" -}}
{{- include "loom.suffixed" (dict "root" . "suffix" "extractor") }}
{{- end }}

{{- define "loom.extractorSelectorLabels" -}}
app.kubernetes.io/name: {{ include "loom.name" . | trunc 53 | trimSuffix "-" }}-extractor
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "loom.assistantName" -}}
{{- include "loom.suffixed" (dict "root" . "suffix" "librechat") }}
{{- end }}

{{- define "loom.assistantSelectorLabels" -}}
app.kubernetes.io/name: {{ include "loom.name" . | trunc 53 | trimSuffix "-" }}-librechat
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "loom.assistantMongoName" -}}
{{- include "loom.suffixed" (dict "root" . "suffix" "librechat-mongo" "max" 52) }}
{{- end }}

{{- define "loom.assistantMongoSelectorLabels" -}}
app.kubernetes.io/name: {{ include "loom.name" . | trunc 47 | trimSuffix "-" }}-librechat-mongo
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "loom.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "loom.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{- define "loom.secretName" -}}
{{- default (include "loom.fullname" .) .Values.secrets.existingSecret }}
{{- end }}

{{- define "loom.image" -}}
{{- if .Values.image.digest }}
{{- printf "%s@%s" .Values.image.repository .Values.image.digest }}
{{- else }}
{{- printf "%s:%s" .Values.image.repository (default .Chart.AppVersion .Values.image.tag) }}
{{- end }}
{{- end }}

{{- define "loom.chHost" -}}
{{- if .Values.devClickhouse.enabled }}
{{- include "loom.suffixed" (dict "root" . "suffix" "clickhouse" "max" 52) }}
{{- else }}
{{- required "clickhouse.host is required (or devClickhouse.enabled for a test cluster)" .Values.clickhouse.host }}
{{- end }}
{{- end }}

{{- /* The settings every pod must share, as env names: the embedding space and the root
secret. A JSON array: `include "loom.guardedNames" . | fromJsonArray`. */}}
{{- define "loom.guardedNames" -}}
["LOOM_EMBEDDING_PROVIDER", "LOOM_EMBEDDING_MODEL", "LOOM_EMBEDDING_DIM", "LOOM_EMBEDDING_BASE_URL", "LOOM_EMBEDDING_SEND_DIMENSIONS", "LOOM_JWT_SECRET", "LOOM_JWT_SECRET_PREVIOUS"]
{{- end -}}

{{- /* Where a setting the chart guards is set instead: the root secret and its previous
value through secrets.* (or the existingSecret, which then holds them, unchecked by the
chart), the embedding space through embedding.*. Takes (dict "key" <env or
settings-file name> "root" $). */}}
{{- define "loom.guardedThrough" -}}
{{- $key := upper .key | trimPrefix "LOOM_" -}}
{{- $values := dict "JWT_SECRET" "secrets.jwtSecret" "JWT_SECRET_PREVIOUS" "secrets.jwtSecretPrevious" -}}
{{- if not (hasKey $values $key) -}}embedding.*, which the chart guards
{{- else if .root.Values.secrets.existingSecret -}}the Secret {{ .root.Values.secrets.existingSecret }} (secrets.existingSecret)
{{- else -}}{{ get $values $key }}, which the chart guards
{{- end -}}
{{- end -}}

{{/* The env every Loom process of the release shares. It is written into each
pod template rather than a ConfigMap, so a pod of the previous ReplicaSet that
restarts mid-rollout keeps its own config instead of booting with the new one. */}}
{{- define "loom.config" -}}
{{- $dev := .Values.devClickhouse.enabled -}}
{{- $guarded := include "loom.guardedNames" . | fromJsonArray -}}
{{- range $k, $_ := .Values.env }}
{{- if has $k $guarded }}
{{- fail (printf "set %s through %s, not env" $k (include "loom.guardedThrough" (dict "key" $k "root" $))) }}
{{- end }}
{{- end }}
{{- range .Values.extraEnv }}
{{- if has .name $guarded }}
{{- fail (printf "set %s through %s, not extraEnv" .name (include "loom.guardedThrough" (dict "key" .name "root" $))) }}
{{- end }}
{{- end }}
{{- range $guarded }}
{{- if hasKey $.Values.secrets.extra . }}
{{- fail (printf "set %s through %s, not secrets.extra" . (include "loom.guardedThrough" (dict "key" . "root" $))) }}
{{- end }}
{{- end }}
{{- /* The analytics export's name key reaches the export pod only. */}}
{{- $nameKey := "LOOM_ANALYTICS_EXPORT_NAME_KEY" }}
{{- $nameKeyRefused := printf "set %s through analyticsExport.nameKeySecret, which only the export pod reads, not env, extraEnv, secrets.extra or settingsFile" $nameKey }}
{{- if or (hasKey .Values.env $nameKey) (hasKey .Values.secrets.extra $nameKey) (hasKey (.Values.settingsFile | default dict) "analytics_export_name_key") }}
{{- fail $nameKeyRefused }}
{{- end }}
{{- range .Values.extraEnv }}
{{- if eq .name $nameKey }}
{{- fail $nameKeyRefused }}
{{- end }}
{{- end }}
{{- $pg := include "loom.postgres" . | fromYaml }}
{{- if $pg.host }}
{{- /* The names the postgres block renders: set twice, one would silently win. */}}
{{- $pgNames := list "LOOM_STORAGE_AUTHORITY" "LOOM_PG_DSN" "LOOM_PG_DURABILITY" "LOOM_PG_POOL_MAX_SIZE" "PGPASSWORD" }}
{{- range $pgNames }}
{{- if hasKey $.Values.env . }}{{ fail (printf "set %s through postgres.*, not env, beside postgres.host" .) }}{{ end }}
{{- if hasKey $.Values.secrets.extra . }}{{ fail (printf "set %s through postgres.*, not secrets.extra, beside postgres.host" .) }}{{ end }}
{{- end }}
{{- range .Values.extraEnv }}
{{- if has .name $pgNames }}{{ fail (printf "set %s through postgres.*, not extraEnv, beside postgres.host" .name) }}{{ end }}
{{- end }}
{{- range $k, $_ := .Values.settingsFile | default dict }}
{{- if has (printf "LOOM_%s" (upper $k)) $pgNames }}{{ fail (printf "set %s through postgres.*, not settingsFile, beside postgres.host" $k) }}{{ end }}
{{- end }}
{{- end }}
LOOM_DEPLOYMENT: "1"
LOOM_PUBLIC_BASE_URL: {{ required "publicBaseUrl is required" .Values.publicBaseUrl | quote }}
{{- if not (hasKey .Values.env "LOOM_OAUTH_ISSUER") }}
LOOM_OAUTH_ISSUER: {{ .Values.publicBaseUrl | quote }}
{{- end }}
{{- if and .Values.ingress.enabled (eq .Values.trustedProxies "127.0.0.1") }}
{{- fail "ingress.enabled needs trustedProxies: the ingress controller's or balancer's source range, or every client shares one sign-in limit" }}
{{- end }}
LOOM_TRUSTED_PROXIES: {{ .Values.trustedProxies | quote }}
LOOM_CH_HOST: {{ include "loom.chHost" . | quote }}
LOOM_CH_PORT: {{ ternary 8123 .Values.clickhouse.port $dev | quote }}
LOOM_CH_SECURE: {{ ternary false .Values.clickhouse.secure $dev | quote }}
LOOM_CH_USER: {{ ternary "loom" .Values.clickhouse.user $dev | quote }}
LOOM_CH_CLUSTER: {{ .Values.clickhouse.cluster | quote }}
{{- with .Values.clickhouse.readerUser }}
LOOM_CH_READER_USER: {{ . | quote }}
{{- end }}
LOOM_SHUTDOWN_READY_DELAY_S: {{ .Values.shutdown.readyDelaySeconds | quote }}
LOOM_GRACEFUL_SHUTDOWN_S: {{ .Values.shutdown.gracefulSeconds | quote }}
LOOM_KEEP_ALIVE_S: {{ .Values.keepAliveSeconds | quote }}
{{- if $pg.host }}
# The Postgres memory authority (postgres.*). The DSN holds no password: the driver
# reads PGPASSWORD (loom.pgEnv), and verify-full checks the server against the CA.
LOOM_STORAGE_AUTHORITY: postgres
LOOM_PG_DSN: {{ printf "postgres://%s@%s:%v/%s?sslmode=verify-full&sslrootcert=/etc/loom/pg-ca/%s" $pg.user $pg.host (int $pg.port) $pg.database $pg.caSecret.key | quote }}
LOOM_PG_DURABILITY: {{ $pg.durability | quote }}
LOOM_PG_POOL_MAX_SIZE: {{ $pg.poolMaxSize | quote }}
{{- end }}
{{- $ownUrl := hasKey .Values.env "LOOM_DOCUMENT_EXTRACTOR_URL" }}
{{- range .Values.extraEnv }}{{ if eq .name "LOOM_DOCUMENT_EXTRACTOR_URL" }}{{ $ownUrl = true }}{{ end }}{{ end }}
{{- if and .Values.extractor.enabled (not $ownUrl) }}
# The release's extractor (extractor.yaml); its token is in the release Secret.
LOOM_DOCUMENT_EXTRACTOR_URL: {{ printf "http://%s:8790" (include "loom.extractorName" .) | quote }}
{{- end }}
{{- $ownAssistant := hasKey .Values.env "LOOM_ASSISTANT_LIBRECHAT_URL" }}
{{- range .Values.extraEnv }}{{ if eq .name "LOOM_ASSISTANT_LIBRECHAT_URL" }}{{ $ownAssistant = true }}{{ end }}{{ end }}
{{- if and .Values.assistant.enabled (not $ownAssistant) }}
# The release's LibreChat (assistant.yaml), which runs the agent Loom seeds.
LOOM_ASSISTANT_LIBRECHAT_URL: {{ printf "http://%s:3080" (include "loom.assistantName" .) | quote }}
{{- end }}
# The web processes' shared files: serve.sh makes $XDG_RUNTIME_DIR/loom, 0700
# and owned by the pod's user (an emptyDir itself is neither).
XDG_RUNTIME_DIR: /run/user
LOOM_CODE_SANDBOX_REQUIRED: {{ .Values.sandbox.required | quote }}
{{- if not (hasKey .Values.env "LOOM_CODE_SCIP_WORK_DIR") }}
# The C++ index and delta (GBs for a large tree) on the cache volume, not /tmp.
LOOM_CODE_SCIP_WORK_DIR: /app/var/scip-work
{{- end }}
{{- with .Values.webWorkers }}
LOOM_WEB_WORKERS: {{ . | quote }}
{{- end }}
{{- with .Values.embedding.provider }}
LOOM_EMBEDDING_PROVIDER: {{ . | quote }}
{{- end }}
LOOM_EMBEDDING_MODEL: {{ .Values.embedding.model | quote }}
LOOM_EMBEDDING_DIM: {{ .Values.embedding.dim | quote }}
{{- with .Values.embedding.baseUrl }}
LOOM_EMBEDDING_BASE_URL: {{ . | quote }}
{{- end }}
LOOM_EMBEDDING_SEND_DIMENSIONS: {{ .Values.embedding.sendDimensions | quote }}
{{- with .Values.llm.provider }}
LOOM_LLM_PROVIDER: {{ . | quote }}
{{- end }}
{{- with .Values.llm.model }}
LOOM_LLM_MODEL: {{ . | quote }}
{{- end }}
{{- range $k, $v := .Values.env }}
{{ $k }}: {{ $v | toString | quote }}
{{- end }}
{{- end }}

{{/* The postgres block, checked: "" host for the ClickHouse authority. A release
upgraded with --reuse-values from a chart without the block has none. */}}
{{- define "loom.postgres" -}}
{{- $pg := .Values.postgres | default dict -}}
{{- if $pg.host -}}
{{- if not (dig "passwordSecret" "name" "" $pg) }}{{ fail "postgres.host needs postgres.passwordSecret.name: the Secret holding the role's password" }}{{ end -}}
{{- if not (dig "caSecret" "name" "" $pg) }}{{ fail "postgres.host needs postgres.caSecret.name: the CA bundle verify-full checks the server's certificate against" }}{{ end -}}
{{- range $field := list "host" "database" "user" }}
{{- if not (and (kindIs "string" (get $pg $field)) (regexMatch "^[A-Za-z0-9._-]+$" (toString (get $pg $field)))) }}{{ fail (printf "postgres.%s goes into the DSN as written: a quoted string of letters, digits, '.', '_' and '-'" $field) }}{{ end }}
{{- end -}}
{{- range $secret := list "passwordSecret" "caSecret" }}
{{- $key := toString (dig $secret "key" "" $pg) }}
{{- if not (and (regexMatch "^[A-Za-z0-9._-]+$" $key) (ne $key ".") (not (hasPrefix ".." $key))) }}{{ fail (printf "postgres.%s.key must be a Secret key: letters, digits, '.', '_' and '-', not '.' or starting '..'" $secret) }}{{ end }}
{{- end -}}
{{- if not (and (regexMatch "^[1-9][0-9]{0,4}$" (toString $pg.port)) (le (int $pg.port) 65535)) }}{{ fail "postgres.port must be a port number, 1 to 65535" }}{{ end -}}
{{- end -}}
{{- toYaml $pg -}}
{{- end }}

{{/* PGPASSWORD for a pod that connects to Postgres. */}}
{{- define "loom.pgEnv" -}}
{{- $pg := include "loom.postgres" . | fromYaml -}}
{{- if $pg.host -}}
- name: PGPASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ $pg.passwordSecret.name | quote }}
      key: {{ $pg.passwordSecret.key | quote }}
{{- end -}}
{{- end }}

{{/* The CA bundle the DSN's sslrootcert names, as a volume and its mount. */}}
{{- define "loom.pgVolume" -}}
{{- $pg := include "loom.postgres" . | fromYaml -}}
{{- if $pg.host -}}
- name: pg-ca
  secret:
    secretName: {{ $pg.caSecret.name | quote }}
    items:
      - key: {{ $pg.caSecret.key | quote }}
        path: {{ $pg.caSecret.key | quote }}
{{- end -}}
{{- end }}

{{- define "loom.pgVolumeMount" -}}
{{- $pg := include "loom.postgres" . | fromYaml -}}
{{- if $pg.host -}}
- name: pg-ca
  mountPath: /etc/loom/pg-ca
  readOnly: true
{{- end -}}
{{- end }}

{{/* Hash of the embedding space, which every pod must share and cannot roll.
The root secret is compared with the release's Secret instead (deployment.yaml),
so no digest of it is published on the Deployment. */}}
{{- define "loom.mustMatch" -}}
{{- $e := .Values.embedding -}}
{{- list $e.provider $e.model (toString $e.dim) $e.baseUrl (toString $e.sendDimensions) | join "\n" | sha256sum | trunc 32 }}
{{- end }}

{{/* mustMatchChange.publish is a token: an upgrade publishes when it differs from
the token the live pods carry, so a value kept by --reuse-values publishes
once. "true" when this upgrade publishes. */}}
{{- define "loom.publishing" -}}
{{- $token := toString (.Values.mustMatchChange.publish | default "") -}}
{{- if and $token (ne $token (include "loom.livePublished" .)) }}true{{ end -}}
{{- end }}

{{/* The token the live web pods carry ("" before any publish). */}}
{{- define "loom.livePublished" -}}
{{- $live := lookup "apps/v1" "Deployment" .Release.Namespace (include "loom.fullname" .) | default dict -}}
{{- dig "spec" "template" "metadata" "annotations" "loom.clickhouse.com/published" "" $live -}}
{{- end }}

{{/* The token the pods carry from now on: the new one, else the live one, so
an ordinary upgrade after a publish rolls nothing it does not change. None on
install, which publishes nothing (hooks run on upgrade only). */}}
{{- define "loom.publishedToken" -}}
{{- if not .Release.IsInstall -}}
{{- toString (.Values.mustMatchChange.publish | default "") | default (include "loom.livePublished" .) -}}
{{- end -}}
{{- end }}

{{/* Rolls the pods when a key of the chart's Secret changes. Keyed with the
root secret, so the pod template carries nothing to test a guess of any one
key against; a root change itself rolls through the publish token. */}}
{{- define "loom.secretChecksum" -}}
{{- $s := .Values.secrets -}}
{{- $keys := dict "ch" (ternary .Values.devClickhouse.password $s.clickhousePassword .Values.devClickhouse.enabled) "extra" $s.extra "previous" $s.jwtSecretPrevious "extractor" (ternary $s.extractorToken "" .Values.extractor.enabled) -}}
{{- printf "%s\n%s" $s.jwtSecret ($keys | toJson) | sha256sum -}}
{{- end }}

{{/* The pod's name labels its ledger rows. Every pod of the deployment shares
the `deploy:k8s:` family, so a new workers leader finds the previous leader's
passes and runs an overdue worker instead of restarting its interval. */}}
{{- define "loom.podEnv" -}}
- name: POD_NAME
  valueFrom:
    fieldRef:
      fieldPath: metadata.name
- name: POD_NAMESPACE
  valueFrom:
    fieldRef:
      fieldPath: metadata.namespace
- name: LOOM_INSTANCE_LABEL
  value: deploy:k8s:$(POD_NAME)
{{- end }}

{{- define "loom.volumeMounts" -}}
- name: tmp
  mountPath: /tmp
- name: runtime
  mountPath: /run/user
- name: cache
  mountPath: /app/var
- name: settings-file
  mountPath: /app/config
  readOnly: true
{{- with include "loom.pgVolumeMount" . }}
{{ . }}
{{- end }}
{{- end }}

{{/* The settings file's `data`, for the release's ConfigMap and each hook Job's copy. */}}
{{- define "loom.settingsFileData" -}}
loom.yaml: |
  # yaml-language-server: $schema=https://raw.githubusercontent.com/ClickHouse/loom/main/schemas/loom-settings.schema.json
  {{- dict "version" 1 "settings" (.Values.settingsFile | default dict) | toYaml | nindent 2 }}
{{- end }}

{{/* A hash of the settings file, so a change to it rolls the pods. */}}
{{- define "loom.settingsFileChecksum" -}}
{{- .Values.settingsFile | toJson | sha256sum -}}
{{- end }}

{{/* The cache settings of one role: `cache`, with `workers.cache` over it
for the workers pods. */}}
{{- define "loom.cacheFor" -}}
{{- if .workers -}}
{{- mergeOverwrite (deepCopy .root.Values.cache) (deepCopy (.root.Values.workers.cache | default dict)) | toYaml -}}
{{- else -}}
{{- .root.Values.cache | toYaml -}}
{{- end -}}
{{- end }}

{{/* The pod's volumes; `workers` picks that role's cache. */}}
{{- define "loom.volumes" -}}
{{- $cache := fromYaml (include "loom.cacheFor" .) -}}
{{- with .root -}}
- name: tmp
  emptyDir:
    sizeLimit: {{ .Values.tmpSizeLimit }}
- name: runtime
  emptyDir:
    medium: Memory
    sizeLimit: 64Mi
{{- if eq $cache.volume "ephemeral" }}
- name: cache
  ephemeral:
    volumeClaimTemplate:
      metadata:
        labels:
          {{- include "loom.selectorLabels" . | nindent 10 }}
      spec:
        accessModes: [ReadWriteOnce]
        {{- with $cache.ephemeral.storageClassName }}
        storageClassName: {{ . }}
        {{- end }}
        {{- with $cache.ephemeral.dataSourceRef }}
        dataSourceRef:
          {{- toYaml . | nindent 10 }}
        {{- end }}
        resources:
          requests:
            storage: {{ $cache.ephemeral.size }}
{{- else if eq $cache.volume "emptyDir" }}
- name: cache
  emptyDir:
    sizeLimit: {{ $cache.sizeLimit }}
{{- else }}
{{- fail "cache.volume must be emptyDir or ephemeral" }}
{{- end }}
- name: settings-file
  configMap:
    name: {{ include "loom.suffixed" (dict "root" . "suffix" "settings") }}
{{- with include "loom.pgVolume" . }}
{{ . }}
{{- end }}
{{- with .Values.extraVolumes }}
{{ toYaml . }}
{{- end }}
{{- end }}
{{- end }}

{{- define "loom.terminationGrace" -}}
{{- $s := .Values.shutdown -}}
{{- add (int $s.readyDelaySeconds) (int $s.gracefulSeconds) (int $s.drainSeconds) }}
{{- end }}

{{- define "loom.probes" -}}
startupProbe:
  httpGet: { path: /v1/health/ready, port: http }
  periodSeconds: {{ .Values.probes.startup.periodSeconds }}
  failureThreshold: {{ .Values.probes.startup.failureThreshold }}
  timeoutSeconds: 3
livenessProbe:
  httpGet: { path: /v1/health/live, port: http }
  periodSeconds: {{ .Values.probes.liveness.periodSeconds }}
  failureThreshold: {{ .Values.probes.liveness.failureThreshold }}
  timeoutSeconds: 3
readinessProbe:
  httpGet: { path: /v1/health/ready, port: http }
  periodSeconds: {{ .Values.probes.readiness.periodSeconds }}
  failureThreshold: {{ .Values.probes.readiness.failureThreshold }}
  timeoutSeconds: 3
{{- end }}

{{/* The container's resources, with an ephemeral-storage request for the
pod's emptyDirs (/tmp, and the cache when it is one) unless one is set, so
the scheduler places the pod where they fit. Sizes in Gi only. */}}
{{- define "loom.resources" -}}
{{- $v := .root.Values -}}
{{- $cache := fromYaml (include "loom.cacheFor" (dict "root" .root "workers" .workers)) -}}
{{- $r := deepCopy (.resources | default dict) -}}
{{- $req := $r.requests | default dict -}}
{{- $lim := $r.limits | default dict -}}
{{- if not (or (hasKey $req "ephemeral-storage") (hasKey $lim "ephemeral-storage")) -}}
{{- $sizes := list (toString $v.tmpSizeLimit) -}}
{{- if eq $cache.volume "emptyDir" }}{{ $sizes = append $sizes (toString $cache.sizeLimit) }}{{ end -}}
{{- $gi := 0 -}}
{{- range $sizes }}
{{- if not (regexMatch "^[0-9]+Gi$" .) }}{{ fail (printf "tmpSizeLimit and cache.sizeLimit are whole Gi (e.g. 20Gi), not %q: or set %s.requests.ephemeral-storage" . $.path) }}{{ end }}
{{- $gi = add $gi (atoi (trimSuffix "Gi" .)) }}
{{- end -}}
{{- $_ := set $req "ephemeral-storage" (printf "%dGi" (int $gi)) -}}
{{- $_ := set $r "requests" $req -}}
{{- end -}}
{{- toYaml $r -}}
{{- end }}

{{/* The container both Deployments run; `serves` false for the workers. */}}
{{- define "loom.container" -}}
{{- $serves := .serves -}}
{{- $ := .root -}}
- name: loom
  image: {{ include "loom.image" $ }}
  imagePullPolicy: {{ $.Values.image.pullPolicy }}
  ports:
    - name: http
      containerPort: 7777
      protocol: TCP
  envFrom:
    - secretRef:
        name: {{ include "loom.secretName" $ }}
    {{- with $.Values.envFrom }}
    {{- toYaml . | nindent 4 }}
    {{- end }}
  env:
    {{- range $k, $v := fromYaml (include "loom.config" $) }}
    {{- if or $serves (ne $k "LOOM_WEB_WORKERS") }}
    - name: {{ $k }}
      value: {{ $v | toString | quote }}
    {{- end }}
    {{- end }}
    {{- include "loom.podEnv" $ | nindent 4 }}
    {{- with include "loom.pgEnv" $ }}
    {{- . | nindent 4 }}
    {{- end }}
    - name: LOOM_SETTINGS_FILE
      value: /app/config/loom.yaml
    {{- if .serves }}
    - name: LOOM_BACKGROUND_WORKERS
      value: {{ ternary "off" "auto" $.Values.workers.enabled | quote }}
    {{- else }}
    - name: LOOM_SERVES_TRAFFIC
      value: "false"
    - name: LOOM_WEB_WORKERS
      value: "1"
    - name: LOOM_BACKGROUND_WORKERS
      value: auto
    - name: LOOM_WORKERS_PROCESS
      value: inline
    {{- end }}
    {{- if not (hasKey $.Values.env "LOOM_CODE_SCIP_CLANG_JOBS") }}
    # A web pod that leads the workers runs one scip-clang job inside its limits.
    - name: LOOM_CODE_SCIP_CLANG_JOBS
      value: {{ ternary $.Values.workers.scipClangJobs 1 (not $serves) | quote }}
    {{- end }}
    {{- with $.Values.extraEnv }}
    {{- toYaml . | nindent 4 }}
    {{- end }}
  {{- include "loom.probes" $ | nindent 2 }}
  resources:
    {{- include "loom.resources" (dict "root" $ "workers" (not $serves) "resources" .resources "path" .resourcesPath) | nindent 4 }}
  securityContext:
    {{- mergeOverwrite (deepCopy $.Values.securityContext) $.Values.sandbox.securityContext | toYaml | nindent 4 }}
  volumeMounts:
    {{- include "loom.volumeMounts" $ | nindent 4 }}
    {{- with $.Values.extraVolumeMounts }}
    {{- toYaml . | nindent 4 }}
    {{- end }}
{{- end }}

{{/* The bundled ClickHouse starts with the release: wait for it rather than
crash-loop. An external ClickHouse is up already. */}}
{{- define "loom.waitForDevClickhouse" -}}
{{- if .Values.devClickhouse.enabled }}
initContainers:
  - name: wait-for-clickhouse
    image: {{ include "loom.image" . }}
    imagePullPolicy: {{ .Values.image.pullPolicy }}
    command:
      - python
      - -c
      - |
        import time, urllib.request
        while True:
            try:
                urllib.request.urlopen("http://{{ include "loom.chHost" . }}:8123/ping", timeout=3)
                break
            except OSError:
                time.sleep(2)
    resources:
      requests: { cpu: 10m, memory: 32Mi }
      limits: { cpu: 100m, memory: 64Mi }
    securityContext:
      {{- toYaml .Values.securityContext | nindent 6 }}
{{- end }}
{{- end }}

{{/* Pod spec fields the Deployments, the hook Jobs and the analytics export
share; `serviceAccount` names another account than the release's. */}}
{{/* `leads`: the pod may hold the workers lease, which alone reads the
Kubernetes API (retireLostPods). */}}
{{- define "loom.podCommon" -}}
{{- $leads := .leads -}}
{{- $sa := .serviceAccount -}}
{{- with .root -}}
serviceAccountName: {{ default (include "loom.serviceAccountName" .) $sa }}
automountServiceAccountToken: {{ or .Values.serviceAccount.automountToken (and .Values.retireLostPods $leads) }}
# Service links would inject LOOM_SERVICE_HOST and the like into Loom's env namespace.
enableServiceLinks: false
{{- if not .Values.sandbox.hostUsers }}
hostUsers: false
{{- else if eq (dig "procMount" "" .Values.sandbox.securityContext) "Unmasked" }}
{{- fail "sandbox.securityContext.procMount: Unmasked needs sandbox.hostUsers: false" }}
{{- end }}
terminationGracePeriodSeconds: {{ include "loom.terminationGrace" . }}
securityContext:
  {{- toYaml .Values.podSecurityContext | nindent 2 }}
{{- with .Values.imagePullSecrets }}
imagePullSecrets:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- with .Values.priorityClassName }}
priorityClassName: {{ . }}
{{- end }}
{{- with .Values.nodeSelector }}
nodeSelector:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- with .Values.tolerations }}
tolerations:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- end }}
{{- end }}

{{/* Every address but `networkPolicy.egressExcept`, as NetworkPolicy peers. */}}
{{- define "loom.anywhereBut" -}}
{{- $v4 := list }}{{- $v6 := list }}
{{- range .Values.networkPolicy.egressExcept }}
{{- if contains ":" . }}{{ $v6 = append $v6 . }}{{ else }}{{ $v4 = append $v4 . }}{{ end }}
{{- end -}}
- ipBlock:
    cidr: 0.0.0.0/0
    {{- with $v4 }}
    except:
      {{- toYaml . | nindent 6 }}
    {{- end }}
- ipBlock:
    cidr: ::/0
    {{- with $v6 }}
    except:
      {{- toYaml . | nindent 6 }}
    {{- end }}
{{- end }}

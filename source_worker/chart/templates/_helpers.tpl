{{- define "loom-source-worker.name" -}}
{{- if not (regexMatch "^[a-z0-9]([-a-z0-9]*[a-z0-9])?$" .Release.Name) -}}
{{- fail "source worker release name must be a DNS label" -}}
{{- end -}}
{{- printf "sw-%s" .Release.Name -}}
{{- end -}}

{{- define "loom-source-worker.selector" -}}
app.kubernetes.io/name: loom-source-worker
app.kubernetes.io/instance: {{ .Release.Name | quote }}
{{- end -}}

{{- define "loom-source-worker.containerSecurity" -}}
allowPrivilegeEscalation: false
readOnlyRootFilesystem: true
capabilities:
  drop: [ALL]
{{- end -}}

{{- define "loom-source-worker.podPeer" -}}
namespaceSelector:
  matchLabels:
    kubernetes.io/metadata.name: {{ .namespace | quote }}
podSelector:
  matchLabels:
    {{- range $label, $value := .matchLabels }}
    {{ $label | quote }}: {{ $value | quote }}
    {{- end }}
{{- end -}}

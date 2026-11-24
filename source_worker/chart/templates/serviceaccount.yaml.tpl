{{- if .Values.enabled }}
apiVersion: v1
kind: ServiceAccount
metadata:
  name: {{ include "loom-source-worker.name" . }}
  namespace: {{ .Release.Namespace | quote }}
automountServiceAccountToken: false
{{- end }}

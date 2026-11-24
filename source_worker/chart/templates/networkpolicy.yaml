{{- if .Values.enabled }}
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: {{ include "loom-source-worker.name" . }}
  namespace: {{ .Release.Namespace | quote }}
spec:
  podSelector:
    matchLabels:
      {{- include "loom-source-worker.selector" . | nindent 6 }}
  policyTypes: [Ingress, Egress]
  ingress:
    - from:
        {{- range .Values.network.callers }}
        - {{ include "loom-source-worker.podPeer" . | nindent 10 | trim }}
        {{- end }}
      ports:
        - port: 8443
          protocol: TCP
  egress:
    - to:
        {{- range .Values.network.dns.nameservers }}
        - ipBlock:
            cidr: {{ printf "%s/32" . | quote }}
        {{- end }}
        {{- range .Values.network.dns.pods }}
        - {{ include "loom-source-worker.podPeer" . | nindent 10 | trim }}
        {{- end }}
      ports:
        - port: 53
          protocol: UDP
        - port: 53
          protocol: TCP
    {{- range .Values.network.httpsPods }}
    - to:
        - {{ include "loom-source-worker.podPeer" . | nindent 10 | trim }}
      ports:
        - port: {{ .port }}
          protocol: TCP
    {{- end }}
    {{- range .Values.network.httpsCidrs }}
    - to:
        - ipBlock:
            cidr: {{ .cidr | quote }}
      ports:
        - port: {{ .port }}
          protocol: TCP
    {{- end }}
{{- end }}

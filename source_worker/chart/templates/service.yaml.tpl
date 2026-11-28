{{- if .Values.enabled }}
apiVersion: v1
kind: Service
metadata:
  name: {{ include "loom-source-worker.name" . }}
  namespace: {{ .Release.Namespace | quote }}
spec:
  type: ClusterIP
  ipFamilyPolicy: SingleStack
  ipFamilies: [IPv4]
  selector:
    {{- include "loom-source-worker.selector" . | nindent 4 }}
  ports:
    - name: https
      port: 8443
      targetPort: https
      protocol: TCP
{{- end }}

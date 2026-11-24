{{- if .Values.enabled }}
{{- $name := include "loom-source-worker.name" . }}
{{- $proxy := .Files.Get "files/proxy.conf" }}
{{- if not $proxy }}
{{- fail "source worker chart is missing files/proxy.conf" }}
{{- end }}
{{- if eq .Values.inputs.vaultTokenSecret .Values.inputs.proxyTlsSecret }}
{{- fail "vaultTokenSecret and proxyTlsSecret must name separate Secrets" }}
{{- end }}
{{- $proxyName := printf "%s-proxy-%s" $name ($proxy | sha256sum | trunc 16) }}
apiVersion: v1
kind: ConfigMap
metadata:
  name: {{ $proxyName }}
  namespace: {{ .Release.Namespace | quote }}
immutable: true
binaryData:
  nginx.conf: {{ $proxy | b64enc | quote }}
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ $name }}
  namespace: {{ .Release.Namespace | quote }}
spec:
  replicas: 1
  strategy:
    type: Recreate
  selector:
    matchLabels:
      {{- include "loom-source-worker.selector" . | nindent 6 }}
  template:
    metadata:
      labels:
        {{- include "loom-source-worker.selector" . | nindent 8 }}
    spec:
      os:
        name: linux
      nodeSelector:
        kubernetes.io/os: linux
      serviceAccountName: {{ $name }}
      automountServiceAccountToken: false
      enableServiceLinks: false
      terminationGracePeriodSeconds: 35
      dnsPolicy: None
      dnsConfig:
        nameservers:
          {{- range .Values.network.dns.nameservers }}
          - {{ . | quote }}
          {{- end }}
      securityContext:
        runAsNonRoot: true
        runAsUser: 10001
        runAsGroup: 10001
        fsGroup: 10001
        seccompProfile:
          type: RuntimeDefault
      initContainers:
        - name: private-directories
          image: {{ .Values.images.worker | quote }}
          command: [python, -IB, -c]
          args:
            - |
              import os
              import stat
              from pathlib import Path

              for directory in (Path('/socket-volume/socket'), Path('/proxy-volume/tmp')):
                  directory.mkdir(mode=0o700, exist_ok=True)
                  info = directory.lstat()
                  if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
                      raise SystemExit('invalid private volume directory')
                  directory.chmod(0o700)
          securityContext:
            {{- include "loom-source-worker.containerSecurity" . | nindent 12 }}
          resources:
            requests: {cpu: 25m, memory: 16Mi, ephemeral-storage: 8Mi}
            limits: {cpu: 100m, memory: 32Mi, ephemeral-storage: 16Mi}
          volumeMounts:
            - {name: socket, mountPath: /socket-volume}
            - {name: proxy-tmp, mountPath: /proxy-volume}
      containers:
        - name: source-worker
          image: {{ .Values.images.worker | quote }}
          command: [python, -IBu, -m, loom_source_worker.main]
          securityContext:
            {{- include "loom-source-worker.containerSecurity" . | nindent 12 }}
          readinessProbe:
            exec:
              command:
                - python
                - -IB
                - -c
                - |
                  import socket
                  with socket.socket(socket.AF_UNIX) as probe:
                      probe.settimeout(1)
                      probe.connect('/run/loom-source-worker/socket/worker.sock')
                  with socket.create_connection(('127.0.0.1', 8443), timeout=0.5):
                      pass
            timeoutSeconds: 2
            periodSeconds: 5
          resources:
            requests: {cpu: 100m, memory: 256Mi, ephemeral-storage: 8Mi}
            limits: {cpu: "1", memory: 512Mi, ephemeral-storage: 16Mi}
          volumeMounts:
            - name: worker-config
              mountPath: /etc/loom-source-worker
              readOnly: true
            - name: vault-token
              mountPath: /run/loom-source-worker/credentials
              readOnly: true
            - name: socket
              mountPath: /run/loom-source-worker/socket
              subPath: socket
        - name: source-worker-proxy
          image: {{ .Values.images.proxy | quote }}
          command: [nginx, -e, /dev/null, -g, "daemon off;", -c, /etc/nginx/nginx.conf]
          ports:
            - {name: https, containerPort: 8443, protocol: TCP}
          securityContext:
            {{- include "loom-source-worker.containerSecurity" . | nindent 12 }}
          resources:
            requests: {cpu: 50m, memory: 64Mi, ephemeral-storage: 8Mi}
            limits: {cpu: 500m, memory: 128Mi, ephemeral-storage: 16Mi}
          volumeMounts:
            - name: proxy-config
              mountPath: /etc/nginx/nginx.conf
              subPath: nginx.conf
              readOnly: true
            - name: proxy-tls
              mountPath: /etc/nginx/tls
              readOnly: true
            - name: socket
              mountPath: /run/loom-source-worker/socket
              subPath: socket
              readOnly: true
            - name: proxy-tmp
              mountPath: /tmp
              subPath: tmp
      volumes:
        - name: worker-config
          configMap:
            name: {{ .Values.inputs.workerConfigMap | quote }}
            optional: false
            defaultMode: 288
            items:
              - {key: config.json, path: config.json}
              - {key: grant-verifier.pem, path: grant-verifier.pem}
              - {key: vault-ca.pem, path: vault-ca.pem}
        - name: vault-token
          secret:
            secretName: {{ .Values.inputs.vaultTokenSecret | quote }}
            optional: false
            defaultMode: 288
            items:
              - {key: vault-token, path: vault-token}
        - name: proxy-config
          configMap:
            name: {{ $proxyName }}
            optional: false
            defaultMode: 288
            items:
              - {key: nginx.conf, path: nginx.conf}
        - name: proxy-tls
          secret:
            secretName: {{ .Values.inputs.proxyTlsSecret | quote }}
            optional: false
            defaultMode: 288
            items:
              - {key: server-chain.pem, path: server-chain.pem}
              - {key: server-key.pem, path: server-key.pem}
              - {key: client-ca.pem, path: client-ca.pem}
        - name: socket
          emptyDir: {medium: Memory, sizeLimit: 64Ki}
        - name: proxy-tmp
          emptyDir: {medium: Memory, sizeLimit: 1Mi}
{{- end }}

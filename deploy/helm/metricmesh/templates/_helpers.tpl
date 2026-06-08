{{/* Common labels applied to every object. */}}
{{- define "metricmesh.labels" -}}
app.kubernetes.io/name: metricmesh
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: metricmesh
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
{{- end -}}

{{/* Selector labels for a component. Call with (dict "root" . "component" "api"). */}}
{{- define "metricmesh.selectorLabels" -}}
app.kubernetes.io/name: metricmesh
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{/* Fully-qualified name for a component, e.g. <release>-api. */}}
{{- define "metricmesh.fullname" -}}
{{- printf "%s-%s" .root.Release.Name .component | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/* The app container image reference. */}}
{{- define "metricmesh.image" -}}
{{ .Values.image.repository }}:{{ .Values.image.tag | default .Chart.AppVersion }}
{{- end -}}

{{/* envFrom block shared by every app pod (ConfigMap + Secret). */}}
{{- define "metricmesh.envFrom" -}}
- configMapRef:
    name: {{ .Release.Name }}-config
- secretRef:
    name: {{ .Release.Name }}-secrets
{{- end -}}

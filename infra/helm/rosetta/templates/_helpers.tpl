{{/* Name of the chart, or nameOverride. */}}
{{- define "rosetta.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/* Name that prefixes every resource: the release name, unless it already contains the chart name. */}}
{{- define "rosetta.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 50 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 50 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 50 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{- define "rosetta.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/* Labels that select the pods of this release. Never change them after the first install. */}}
{{- define "rosetta.selectorLabels" -}}
app.kubernetes.io/name: {{ include "rosetta.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "rosetta.labels" -}}
helm.sh/chart: {{ include "rosetta.chart" . }}
{{ include "rosetta.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: rosetta
{{- end }}

{{/* Image reference. A digest wins over the tag, the tag defaults to the appVersion. */}}
{{- define "rosetta.image" -}}
{{- if .Values.image.digest }}
{{- printf "%s@%s" .Values.image.repository .Values.image.digest }}
{{- else }}
{{- printf "%s:%s" .Values.image.repository (default .Chart.AppVersion .Values.image.tag) }}
{{- end }}
{{- end }}

{{- define "rosetta.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "rosetta.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/* The non-secret settings as "KEY: value" lines. Used by the ConfigMap and by the ConfigMap of the hooks. */}}
{{- define "rosetta.configData" -}}
{{- range $key, $value := .Values.config }}
{{ $key }}: {{ $value | toString | quote }}
{{- end }}
{{- end }}

variable "project_id" {
  description = "Google Cloud Project ID hosting JackieAgent and Secret Manager"
  type        = string
}

variable "region" {
  description = "GCP deployment region"
  type        = string
  default     = "us-east1"
}

variable "service_name" {
  description = "Cloud Run v2 service name"
  type        = string
  default     = "jackie-half-marathon-agent"
}

variable "container_image" {
  description = "Artifact Registry container image URI"
  type        = string
}

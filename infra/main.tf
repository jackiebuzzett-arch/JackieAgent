terraform {
  required_version = ">= 1.5.0"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.40"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

# Dedicated least-privilege Service Account for JackieAgent
resource "google_service_account" "agent_sa" {
  account_id   = "${var.service_name}-sa"
  display_name = "JackieAgent Multi-Agent Coach Runtime SA"
}

# Google Cloud Secret Manager secrets (no hardcoded credentials)
resource "google_secret_manager_secret" "oura_client_id" {
  secret_id = "oura-client-id"
  replication {
    auto {}
  }
}

resource "google_secret_manager_secret" "oura_client_secret" {
  secret_id = "oura-client-secret"
  replication {
    auto {}
  }
}

resource "google_secret_manager_secret" "gemini_api_key" {
  secret_id = "gemini-api-key"
  replication {
    auto {}
  }
}

# Grant Secret Accessor + Cloud Trace / Logging telemetry roles
resource "google_secret_manager_secret_iam_member" "oura_id_access" {
  secret_id = google_secret_manager_secret.oura_client_id.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.agent_sa.email}"
}

resource "google_secret_manager_secret_iam_member" "oura_secret_access" {
  secret_id = google_secret_manager_secret.oura_client_secret.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.agent_sa.email}"
}

resource "google_secret_manager_secret_iam_member" "gemini_key_access" {
  secret_id = google_secret_manager_secret.gemini_api_key.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.agent_sa.email}"
}

resource "google_project_iam_member" "trace_agent" {
  project = var.project_id
  role    = "roles/cloudtrace.agent"
  member  = "serviceAccount:${google_service_account.agent_sa.email}"
}

resource "google_project_iam_member" "log_writer" {
  project = var.project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.agent_sa.email}"
}

# Cloud Run v2 Service running JackieAgent
resource "google_cloud_run_v2_service" "jackie_agent" {
  name     = var.service_name
  location = var.region
  ingress  = "INGRESS_TRAFFIC_ALL"

  template {
    service_account = google_service_account.agent_sa.email

    containers {
      image = var.container_image

      ports {
        container_port = 8080
      }

      env {
        name  = "GCP_PROJECT_ID"
        value = var.project_id
      }

      env {
        name = "OURA_CLIENT_ID"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.oura_client_id.secret_id
            version = "latest"
          }
        }
      }

      env {
        name = "OURA_CLIENT_SECRET"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.oura_client_secret.secret_id
            version = "latest"
          }
        }
      }

      env {
        name = "GEMINI_API_KEY"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.gemini_api_key.secret_id
            version = "latest"
          }
        }
      }

      startup_probe {
        http_get {
          path = "/healthz"
          port = 8080
        }
      }
    }
  }
}

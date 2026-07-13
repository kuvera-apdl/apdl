use axum::{routing::get, Json, Router};
use serde_json::{json, Value};

async fn health() -> Json<Value> {
    Json(json!({"status": "ok", "service": "codegen"}))
}

fn router() -> Router {
    Router::new().route("/health/codegen", get(health))
}

fn main() {
    let _ = router();
}

fn router() -> Router {
    Router::new().route("/health/codegen", get(health))
}

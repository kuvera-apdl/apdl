def disposition(failure):
    if failure == "runner_timeout":
        return "infrastructure_rerun"
    return "product_code_repair"

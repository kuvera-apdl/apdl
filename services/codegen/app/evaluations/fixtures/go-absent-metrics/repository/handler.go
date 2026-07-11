package fixture

func Complete(job Job, metrics Metrics) {
	metrics.RecordCompleted(job.ID)
}

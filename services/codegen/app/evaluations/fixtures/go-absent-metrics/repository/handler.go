package fixture

type Job struct {
	ID string
}

type Metrics interface {
	RecordCompleted(string)
}

func Complete(job Job, metrics Metrics) {
	metrics.RecordCompleted(job.ID)
}

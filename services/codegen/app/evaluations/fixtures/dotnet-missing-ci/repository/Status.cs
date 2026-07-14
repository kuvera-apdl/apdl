public static class Status
{
    public static string FromCheckCount(int checks)
    {
        if (checks == 0) return "unverified_external_ci";
        return "ci_running";
    }
}

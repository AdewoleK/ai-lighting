using LightingAI.Models;
using Newtonsoft.Json;
using System;
using System.IO;
using System.Net.Http;
using System.Threading;

namespace LightingAI.Services
{
    /// <summary>
    /// Thin synchronous wrapper around the Python FastAPI backend.
    /// AutoCAD commands run on the main editor thread, so we use
    /// GetAwaiter().GetResult() to block in-place; this is intentional
    /// and safe inside a [CommandMethod] handler.
    /// </summary>
    internal static class ApiClient
    {
        private static readonly HttpClient _http = new HttpClient
        {
            Timeout = TimeSpan.FromMinutes(10),
        };

        // Persists across the AutoCAD session; changeable via LIGHTINGAI_SETTINGS
        internal static string BaseUrl { get; set; } = "http://localhost:8000";

        // ── Health check ────────────────────────────────────────────────────────
        internal static bool CheckHealth()
        {
            try
            {
                var r = _http.GetAsync($"{BaseUrl}/health")
                             .GetAwaiter().GetResult();
                return r.IsSuccessStatusCode;
            }
            catch { return false; }
        }

        // ── Submit plan file ────────────────────────────────────────────────────
        internal static ProcessResponse SubmitPlan(
            string dwgPath, string projectName, string customer, string conceptId)
        {
            using var content    = new MultipartFormDataContent();
            using var fileStream = File.OpenRead(dwgPath);
            using var fileBytes  = new StreamContent(fileStream);

            content.Add(fileBytes,                           "file",         Path.GetFileName(dwgPath));
            content.Add(new StringContent(conceptId),        "concept_id");
            content.Add(new StringContent(projectName),      "project_name");
            content.Add(new StringContent(customer),         "customer");

            var response = _http.PostAsync($"{BaseUrl}/process", content)
                                .GetAwaiter().GetResult();
            response.EnsureSuccessStatusCode();

            var json = response.Content.ReadAsStringAsync().GetAwaiter().GetResult();
            return JsonConvert.DeserializeObject<ProcessResponse>(json)!;
        }

        // ── Poll until done or error ────────────────────────────────────────────
        /// <param name="progress">Callback called after every poll with the latest message.</param>
        internal static JobStatusResponse PollJob(
            string jobId, Action<string>? progress = null, CancellationToken ct = default)
        {
            while (!ct.IsCancellationRequested)
            {
                Thread.Sleep(2000);

                var response = _http.GetAsync($"{BaseUrl}/jobs/{jobId}", ct)
                                    .GetAwaiter().GetResult();
                response.EnsureSuccessStatusCode();

                var json   = response.Content.ReadAsStringAsync().GetAwaiter().GetResult();
                var status = JsonConvert.DeserializeObject<JobStatusResponse>(json)!;

                progress?.Invoke(status.Message);

                if (status.Status is "done" or "error") return status;
            }
            throw new OperationCanceledException(ct);
        }
    }
}

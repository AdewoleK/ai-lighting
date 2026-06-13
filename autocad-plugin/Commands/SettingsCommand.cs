using Autodesk.AutoCAD.ApplicationServices;
using Autodesk.AutoCAD.EditorInput;
using Autodesk.AutoCAD.Runtime;
using LightingAI.Services;

namespace LightingAI.Commands
{
    /// <summary>
    /// LIGHTINGAI_SETTINGS
    ///
    /// Changes the base URL of the Python API for this AutoCAD session.
    /// Default: http://localhost:8000
    /// Production example: https://lighting-ai.example.com
    /// </summary>
    public class SettingsCommand
    {
        [CommandMethod("LIGHTINGAI_SETTINGS", CommandFlags.Modal)]
        public void Run()
        {
            var doc = Application.DocumentManager.MdiActiveDocument;
            var ed  = doc.Editor;

            var opt = new PromptStringOptions(
                $"\nAPI base URL [{ApiClient.BaseUrl}]: ")
            {
                DefaultValue = ApiClient.BaseUrl,
                AllowSpaces  = false,
            };
            var result = ed.GetString(opt);
            if (result.Status != PromptStatus.OK) return;

            string url = result.StringResult.TrimEnd('/');
            if (string.IsNullOrWhiteSpace(url)) url = ApiClient.BaseUrl;

            ApiClient.BaseUrl = url;

            bool ok = ApiClient.CheckHealth();
            ed.WriteMessage(
                ok
                    ? $"\n[LightingAI] ✓  API URL set to: {url}\n"
                    : $"\n[LightingAI] ✗  Set URL to: {url}  (backend not reachable — check it is running)\n");
        }
    }
}

using Autodesk.AutoCAD.ApplicationServices;
using Autodesk.AutoCAD.Runtime;
using LightingAI.UI;

// Tells AutoCAD which class to call on load/unload
[assembly: ExtensionApplication(typeof(LightingAI.PluginLoader))]

namespace LightingAI
{
    public class PluginLoader : IExtensionApplication
    {
        public void Initialize()
        {
            var doc = Application.DocumentManager.MdiActiveDocument;
            if (doc != null)
            {
                doc.Editor.WriteMessage(
                    "\n╔══════════════════════════════════════════════╗\n" +
                    "║   LIGHTING AI — MIKA80-E Rossmann v1.0       ║\n" +
                    "║                                              ║\n" +
                    "║  LIGHTINGAI_SETUP    → mark grid origin      ║\n" +
                    "║  LIGHTINGAI_PLACE    → run pipeline + place  ║\n" +
                    "║  LIGHTINGAI_CLEAR    → remove AI layers      ║\n" +
                    "║  LIGHTINGAI_SETTINGS → set API URL           ║\n" +
                    "╚══════════════════════════════════════════════╝\n");
            }

            // Build the ribbon panel (safe to call; skipped if no ribbon present)
            RibbonBuilder.AddRibbonTab();
        }

        public void Terminate() { }
    }
}

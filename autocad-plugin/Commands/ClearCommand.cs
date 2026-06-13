using Autodesk.AutoCAD.ApplicationServices;
using Autodesk.AutoCAD.Runtime;
using LightingAI.Services;

namespace LightingAI.Commands
{
    /// <summary>
    /// LIGHTINGAI_CLEAR
    ///
    /// Removes every entity on an AI-* layer from the current space.
    /// The original floor plan geometry is untouched.
    /// </summary>
    public class ClearCommand
    {
        [CommandMethod("LIGHTINGAI_CLEAR", CommandFlags.Modal)]
        public void Run()
        {
            var doc = Application.DocumentManager.MdiActiveDocument;
            var ed  = doc.Editor;
            var db  = doc.Database;

            using var tr = db.TransactionManager.StartTransaction();
            int removed  = LayerManager.RemoveAiEntities(tr, db);
            tr.Commit();

            ed.WriteMessage($"\n[LightingAI] Removed {removed} AI-placed entities.\n");
        }
    }
}

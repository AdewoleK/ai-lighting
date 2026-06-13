using Autodesk.AutoCAD.ApplicationServices;
using Autodesk.AutoCAD.DatabaseServices;
using Autodesk.AutoCAD.EditorInput;
using Autodesk.AutoCAD.Geometry;
using Autodesk.AutoCAD.Runtime;
using LightingAI.Services;

namespace LightingAI.Commands
{
    /// <summary>
    /// LIGHTINGAI_SETUP
    ///
    /// Lets the designer pick the "Startmaß Rasterdecke" — the exact grid origin
    /// point — directly in AutoCAD.  The coordinate is stored two ways:
    ///
    ///   1. A POINT entity on layer AI-GRID-ORIGIN (visible, snappable)
    ///   2. An XRECORD in the drawing's Named Objects Dictionary under key
    ///      LIGHTINGAI/GRID_ORIGIN  (survives Save/Open; read by LIGHTINGAI_PLACE)
    ///
    /// The Python API also reads the AI-GRID-ORIGIN layer when it parses the
    /// exported DWG, so the origin is transmitted automatically without any
    /// manual form entry.
    /// </summary>
    public class SetupCommand
    {
        [CommandMethod("LIGHTINGAI_SETUP", CommandFlags.Modal)]
        public void Run()
        {
            var doc = Application.DocumentManager.MdiActiveDocument;
            var ed  = doc.Editor;
            var db  = doc.Database;

            ed.WriteMessage("\n[LightingAI] Pick the Startmaß Rasterdecke point (grid origin).\n");
            ed.WriteMessage("[LightingAI] Snap to the intersection of the first visible grid lines.\n\n");

            // ── 1. Pick the origin point ─────────────────────────────────────
            var ppo = new PromptPointOptions("\nStartmaß — pick grid origin: ");
            var ppr = ed.GetPoint(ppo);
            if (ppr.Status != PromptStatus.OK)
            {
                ed.WriteMessage("[LightingAI] Cancelled.\n");
                return;
            }
            Point3d origin = ppr.Value;

            // ── 2. Ask for grid pitch (default 1250 mm for Rossmann) ──────────
            var pio = new PromptIntegerOptions("\nGrid pitch in mm [1250]: ")
            {
                DefaultValue = 1250,
                AllowNegative = false,
                AllowZero = false,
                AllowNone = true,
            };
            var pir   = ed.GetInteger(pio);
            int pitch = pir.Status == PromptStatus.OK ? pir.Value : 1250;

            // ── 3. Write to drawing ───────────────────────────────────────────
            using (var tr = db.TransactionManager.StartTransaction())
            {
                LayerManager.EnsureLayers(tr, db);

                var ms = (BlockTableRecord)tr.GetObject(db.CurrentSpaceId, OpenMode.ForWrite);

                // Remove any previous origin marker
                foreach (ObjectId eid in ms)
                {
                    if (tr.GetObject(eid, OpenMode.ForRead) is Entity ent
                        && ent.Layer == "AI-GRID-ORIGIN")
                    {
                        ent.UpgradeOpen();
                        ent.Erase();
                    }
                }

                // Place a POINT entity (visible as a node / cross)
                var pt = new DBPoint(origin) { Layer = "AI-GRID-ORIGIN" };
                ms.AppendEntity(pt);
                tr.AddNewlyCreatedDBObject(pt, true);

                // Add a small annotation so the drafter can see what was marked
                var txt = new DBText
                {
                    TextString = $"Startmaß Rasterdecke  pitch={pitch}",
                    Position   = new Point3d(origin.X + pitch * 0.05, origin.Y + pitch * 0.05, 0),
                    Height     = pitch * 0.08,
                    Layer      = "AI-GRID-ORIGIN",
                };
                ms.AppendEntity(txt);
                tr.AddNewlyCreatedDBObject(txt, true);

                // Persist origin + pitch in the Named Objects Dictionary
                var nod = (DBDictionary)tr.GetObject(db.NamedObjectsDictionaryId, OpenMode.ForWrite);

                DBDictionary aiDict;
                if (nod.Contains("LIGHTINGAI"))
                    aiDict = (DBDictionary)tr.GetObject(nod.GetAt("LIGHTINGAI"), OpenMode.ForWrite);
                else
                {
                    aiDict = new DBDictionary();
                    nod.SetAt("LIGHTINGAI", aiDict);
                    tr.AddNewlyCreatedDBObject(aiDict, true);
                }

                var xrec = new Xrecord();
                xrec.Data = new ResultBuffer(
                    new TypedValue((int)DxfCode.XCoordinate, origin),
                    new TypedValue((int)DxfCode.Int32,        pitch));
                aiDict.SetAt("GRID_ORIGIN", xrec);
                tr.AddNewlyCreatedDBObject(xrec, true);

                tr.Commit();
            }

            ed.WriteMessage($"\n[LightingAI] ✓ Grid origin set:  X={origin.X:F0}  Y={origin.Y:F0}  pitch={pitch} mm\n");
            ed.WriteMessage("[LightingAI] Run LIGHTINGAI_PLACE to generate the Deckenrasterplan.\n");
        }
    }
}

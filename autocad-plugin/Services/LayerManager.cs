using Autodesk.AutoCAD.Colors;
using Autodesk.AutoCAD.DatabaseServices;
using System;
using System.Collections.Generic;

namespace LightingAI.Services
{
    /// <summary>
    /// Manages the AI-prefixed layers and provides a cleanup routine that
    /// removes every entity that lives on an AI-* layer — making the command
    /// safely re-runnable on the same drawing.
    /// </summary>
    internal static class LayerManager
    {
        // All layers the plugin owns
        private static readonly (string Name, short Aci, int LwHundredths)[] Definitions =
        {
            ("AI-LUMINAIRES",  6,  35),   // magenta
            ("AI-ZONES",       3,  18),   // green
            ("AI-GRID-ORIGIN", 2,  18),   // yellow  — written by LIGHTINGAI_SETUP
            ("AI-DIMENSIONS",  7,  18),
            ("AI-LEGEND",      7,  18),
            ("AI-TITLEBLOCK",  7,  25),
            ("AI-ANNOTATIONS", 9,  13),
        };

        /// <summary>
        /// Creates every AI-* layer if it does not already exist.
        /// Call inside an open transaction before writing any entities.
        /// </summary>
        internal static void EnsureLayers(Transaction tr, Database db)
        {
            var lt = (LayerTable)tr.GetObject(db.LayerTableId, OpenMode.ForRead);
            lt.UpgradeOpen();

            foreach (var (name, aci, lw100) in Definitions)
            {
                if (lt.Has(name)) continue;

                var ltr = new LayerTableRecord
                {
                    Name       = name,
                    Color      = Color.FromColorIndex(ColorMethod.ByAci, aci),
                    LineWeight = (LineWeight)lw100,
                    IsPlottable = true,
                };
                lt.Add(ltr);
                tr.AddNewlyCreatedDBObject(ltr, true);
            }
        }

        /// <summary>
        /// Erases every entity whose layer starts with "AI-" from current space.
        /// Preserves all original floor-plan entities.
        /// </summary>
        internal static int RemoveAiEntities(Transaction tr, Database db)
        {
            var ms    = (BlockTableRecord)tr.GetObject(db.CurrentSpaceId, OpenMode.ForWrite);
            var toErase = new List<ObjectId>();

            foreach (ObjectId eid in ms)
            {
                try
                {
                    if (tr.GetObject(eid, OpenMode.ForRead) is Entity ent
                        && ent.Layer.StartsWith("AI-", StringComparison.OrdinalIgnoreCase))
                        toErase.Add(eid);
                }
                catch { /* skip non-graphical objects */ }
            }

            foreach (var eid in toErase)
            {
                var ent = (Entity)tr.GetObject(eid, OpenMode.ForWrite);
                ent.Erase();
            }
            return toErase.Count;
        }
    }
}

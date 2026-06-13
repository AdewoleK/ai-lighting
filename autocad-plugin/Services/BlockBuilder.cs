using Autodesk.AutoCAD.Colors;
using Autodesk.AutoCAD.DatabaseServices;
using Autodesk.AutoCAD.Geometry;
using System.Collections.Generic;

namespace LightingAI.Services
{
    /// <summary>
    /// Creates (or reuses) block definitions for each MIKA80-E luminaire type.
    ///
    /// Each block contains:
    ///   • Outer circle  — 128 mm cutout diameter (visible in ceiling plan)
    ///   • Inner dot     — 35 % of radius (centre indicator)
    ///   • Cross-hair    — ±50 % radius horizontal + vertical lines
    ///   • ATTDEF TYPE   — invisible; populated per INSERT with lumi type letter
    ///   • ATTDEF PRODUCT— invisible; populated per INSERT with full product code
    ///
    /// Block names follow the pattern "MIKA80E-A", "MIKA80E-B", …
    /// </summary>
    internal static class BlockBuilder
    {
        private const double CutoutMm = 128.0;

        private static readonly Dictionary<string, short> TypeColors = new()
        {
            { "A", 6 },   // magenta
            { "B", 1 },   // red
            { "C", 4 },   // cyan   (accent)
            { "D", 2 },   // yellow (IP44)
            { "E", 5 },   // blue   (pendant)
        };

        /// <summary>
        /// Returns the ObjectId of the block definition, creating it if necessary.
        /// Must be called inside an open transaction.
        /// </summary>
        internal static ObjectId EnsureBlock(
            Transaction tr, Database db, string lumiType, string productCode)
        {
            var bt        = (BlockTable)tr.GetObject(db.BlockTableId, OpenMode.ForRead);
            string name   = $"MIKA80E-{lumiType}";

            if (bt.Has(name)) return bt[name];

            short aci = TypeColors.TryGetValue(lumiType, out var c) ? c : (short)6;
            var   col = Color.FromColorIndex(ColorMethod.ByAci, aci);
            double r  = CutoutMm / 2.0;

            bt.UpgradeOpen();
            var btr = new BlockTableRecord { Name = name, Origin = Point3d.Origin };
            var id  = bt.Add(btr);
            tr.AddNewlyCreatedDBObject(btr, true);

            // ── Outer circle ──────────────────────────────────────────────────
            Add(tr, btr, new Circle(Point3d.Origin, Vector3d.ZAxis, r) { Color = col });

            // ── Inner dot ─────────────────────────────────────────────────────
            Add(tr, btr, new Circle(Point3d.Origin, Vector3d.ZAxis, r * 0.35) { Color = col });

            // ── Cross-hair ────────────────────────────────────────────────────
            Add(tr, btr, new Line(new Point3d(-r * 0.5, 0, 0), new Point3d(r * 0.5, 0, 0))
                { Color = col });
            Add(tr, btr, new Line(new Point3d(0, -r * 0.5, 0), new Point3d(0, r * 0.5, 0))
                { Color = col });

            // ── ATTDEFs (invisible — carried through to every INSERT) ─────────
            Add(tr, btr, new AttributeDefinition
            {
                Tag         = "TYPE",
                Prompt      = "Luminaire Type",
                TextString  = lumiType,
                Position    = new Point3d(0, r * 1.4, 0),
                Height      = r * 0.4,
                Invisible   = true,
                Constant    = false,
                Layer       = "0",
            });
            Add(tr, btr, new AttributeDefinition
            {
                Tag         = "PRODUCT",
                Prompt      = "Product Code",
                TextString  = productCode,
                Position    = new Point3d(0, -r * 1.9, 0),
                Height      = r * 0.35,
                Invisible   = true,
                Constant    = false,
                Layer       = "0",
            });

            return id;
        }

        private static void Add(Transaction tr, BlockTableRecord btr, Entity e)
        {
            e.Layer = "0";   // inherits colour from INSERT override
            btr.AppendEntity(e);
            tr.AddNewlyCreatedDBObject(e, true);
        }
    }
}

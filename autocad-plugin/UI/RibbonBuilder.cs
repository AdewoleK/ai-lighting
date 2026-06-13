using Autodesk.AutoCAD.ApplicationServices;
using Autodesk.Windows;
using System;
using System.Windows.Controls;
using System.Windows.Input;

namespace LightingAI.UI
{
    /// <summary>
    /// Adds a "Lighting AI" tab to the AutoCAD ribbon on plugin load.
    ///
    /// Tab → Panel "MIKA80-E Rossmann" → four large buttons:
    ///   ◎  Set Grid Origin   → LIGHTINGAI_SETUP
    ///   ▶  Place Luminaires  → LIGHTINGAI_PLACE
    ///   ✕  Clear AI Layers   → LIGHTINGAI_CLEAR
    ///   ⚙  Settings          → LIGHTINGAI_SETTINGS
    ///
    /// Safe to call even when no ribbon is present (batch / command-line mode).
    /// </summary>
    internal static class RibbonBuilder
    {
        private const string TabId = "LIGHTINGAI_RIBBON_TAB";

        internal static void AddRibbonTab()
        {
            try
            {
                var rc = ComponentManager.Ribbon;
                if (rc == null) return;

                // Don't add twice
                if (rc.FindTab(TabId) != null) return;

                var tab = new RibbonTab { Title = "Lighting AI", Id = TabId };
                rc.Tabs.Add(tab);

                // ── Panel ────────────────────────────────────────────────────
                var panelSrc = new RibbonPanelSource { Title = "MIKA80-E  Rossmann" };
                var panel    = new RibbonPanel { Source = panelSrc };
                tab.Panels.Add(panel);

                // ── Row 1: Setup + Place ──────────────────────────────────────
                var row1 = new RibbonRowPanel();
                panelSrc.Items.Add(row1);

                row1.Items.Add(Btn(
                    "Set Grid\nOrigin",
                    "LIGHTINGAI_SETUP",
                    "Pick the Startmaß Rasterdecke (grid anchor) directly in the drawing.\n" +
                    "Must be run once before LIGHTINGAI_PLACE."));

                row1.Items.Add(new RibbonSeparator());

                row1.Items.Add(Btn(
                    "Place\nLuminaires",
                    "LIGHTINGAI_PLACE",
                    "Upload the current DWG to the lighting-ai backend, run the full pipeline, " +
                    "and place all MIKA80-E luminaires directly in this drawing."));

                // ── Row 2: Clear + Settings ───────────────────────────────────
                panelSrc.Items.Add(new RibbonRowBreak());
                var row2 = new RibbonRowPanel();
                panelSrc.Items.Add(row2);

                row2.Items.Add(Btn(
                    "Clear\nAI Layers",
                    "LIGHTINGAI_CLEAR",
                    "Remove all AI-placed luminaires, legend, and title block.\n" +
                    "The original floor plan is never touched."));

                row2.Items.Add(new RibbonSeparator());

                row2.Items.Add(Btn(
                    "Settings",
                    "LIGHTINGAI_SETTINGS",
                    "Change the API base URL (default http://localhost:8000).",
                    large: false));

                // Make the new tab active so the designer sees it straight away
                rc.ActiveTab = tab;
            }
            catch (Exception)
            {
                // Ribbon unavailable (e.g. AutoCAD Core Console, batch scripts)
                // — silently continue; commands still work via the command line
            }
        }

        private static RibbonButton Btn(string text, string command,
            string tooltip, bool large = true)
        {
            return new RibbonButton
            {
                Text           = text,
                ToolTip        = new RibbonToolTip { Title = text, Content = tooltip },
                CommandHandler = new AcadCommandHandler(command),
                Size           = large ? RibbonItemSize.Large : RibbonItemSize.Standard,
                ShowText       = true,
                Orientation    = Orientation.Vertical,
            };
        }
    }

    // ── Minimal ICommand that fires an AutoCAD command string ──────────────────
    internal sealed class AcadCommandHandler : ICommand
    {
        private readonly string _cmd;

        internal AcadCommandHandler(string cmd) => _cmd = cmd;

        public event EventHandler? CanExecuteChanged;

        public bool CanExecute(object? parameter) => true;

        public void Execute(object? parameter)
        {
            Application.DocumentManager.MdiActiveDocument
                       ?.SendStringToExecute($"{_cmd} ", activate: true,
                                             wrapUpInactiveDoc: false, echoCmmd: false);
        }
    }
}

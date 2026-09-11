#region Using declarations
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.ComponentModel.DataAnnotations;
using System.Globalization;
using System.IO;
using System.Text;
using System.Windows.Media;
using NinjaTrader.Cbi;
using NinjaTrader.Data;
using NinjaTrader.Gui.Chart;
using NinjaTrader.Gui.Tools;
using NinjaTrader.NinjaScript;
using NinjaTrader.NinjaScript.Indicators.MLExtensionsLib;
using NinjaTrader.NinjaScript.Indicators.KernelFunctionsLib;
using NinjaTrader.NinjaScript.DrawingTools;
#endregion

// ============================================================================
// NQ BOT v3 - SignalLogger
// ----------------------------------------------------------------------------
// Purely observational research indicator. It NEVER submits orders.
// Derived from ALGO_MLScoreOutcomeTester, but the TP/SL virtual-trade logic is
// stripped out. It logs RAW EXCURSIONS + a full point-in-time feature snapshot
// for every qualifying Lorentzian signal, so any exit target can be derived
// post-hoc without re-collecting data (see v3 doc 5.1 label-leakage rule).
//
// Implements the seven locked collection decisions:
//   1. Observation trigger  : every completed 15s bar whose Lorentzian score is
//                             +4..+8 or -4..-8. NO dedup. Consecutive in-band
//                             bars each produce their own observation. Cluster
//                             bookkeeping is recorded instead of throwing rows
//                             away (dedup/weighting handled later in training).
//   2. Filters              : each filter stored individually (pass/fail + raw
//                             value) AND a composite entry_filters_passed.
//                             Filters do NOT gate logging.
//   3. Execution states     : entry_filters_passed, candidate_executed_live and
//                             manual_intervention kept as three separate fields.
//   4. Reference price       : completed 15s signal-bar CLOSE = signal_reference_price.
//                             All research labels are measured from it. Live fill
//                             price / slippage is a SEPARATE later concern.
//   5. Direction-normalized  : favorable/adverse relative to signal direction.
//                             MFE/MAE stored as positive tick magnitudes; the raw
//                             path is preserved (bars file) for full reconstruction.
//   6. Sessions              : all sessions logged and tagged RTH / OVERNIGHT.
//                             (Train RTH-only later by filtering on the tag.)
//   7. IDs                   : deterministic signal_id (stable across replays) +
//                             a run_id identifying this specific logger run.
//
// OUTPUT (two joinable CSVs, InvariantCulture, AutoFlush on for crash safety):
//   signals_<run_id>.csv : one row per observation (snapshot + labels + fields).
//   bars_<run_id>.csv    : each 15s OHLCV logged ONCE. Join on the 60-min window
//                          after signal_reference_price to reconstruct any path.
//
// Apply to a 15-second NQ/MNQ chart. A 1-tick secondary series is added for
// tick-accurate MFE/MAE and MAE-before-MFE sequencing.
// ============================================================================

namespace NinjaTrader.NinjaScript.Indicators
{
    public class SignalLogger : Indicator
    {
        // ---- Source classifier (unchanged score source) --------------------
        private MLLorentzianClassification ml;

        // ---- Research indicator instances (mirror the live filter math) -----
        private ATR atrRecent;      // ATR(1)  - mirrors harness researchAtrRecent
        private ATR atrHistorical;  // ATR(10) - mirrors harness researchAtrHistorical
        private ADX adx;            // ADX(14)
        private RSI rsi;            // RSI(14)
        private CCI cci;            // CCI(20)
        private EMA ema200;
        private EMA ema800;
        private SMA sma200;
        private SMA volAvg;         // average volume
        private RegimeFilterState regime;

        // ---- Kernel history (same recurrence the harness uses) --------------
        private double kernelPrev1 = double.NaN;
        private double kernelPrev2 = double.NaN;
        private double previousAdxValue = double.NaN;

        // ---- Candle-run / prior-bar bookkeeping -----------------------------
        private int sameDirectionCandleRun = 0;
        private int previousCandleDirection = 0;
        private int previousBarScore = 0;         // raw rounded score of the prior 15s bar (any value)

        // ---- Cluster bookkeeping (decision 1) -------------------------------
        // A cluster = a maximal run of consecutive SAME-DIRECTION qualifying bars.
        // It ends when the score returns to neutral (-3..+3) OR flips to the
        // opposite qualifying direction. Neutral bars leave no observation, so
        // within a cluster every bar is an adjacent qualifying same-dir signal.
        private int clusterCounter = 0;           // increments each new cluster -> signal_cluster_id
        private int currentClusterId = 0;
        private int currentClusterDir = 0;        // +1 / -1 / 0 (no active cluster)
        private int clusterStartBar = -1;
        private int clusterMaxAbsScore = 0;
        private int exactScoreRunScore = 0;       // for exact_score_sequence_position
        private int exactScoreRunPos = 0;
        private int lastLongSignalBar = -1;       // last qualifying long bar (across clusters)
        private int lastShortSignalBar = -1;      // last qualifying short bar (across clusters)

        // ---- Session VWAP (reset at RTH open each day) ----------------------
        private DateTime vwapSessionDate = DateTime.MinValue;
        private double vwapCumPV = 0.0;
        private double vwapCumV = 0.0;

        // ---- Forward-tracking observations (awaiting label completion) ------
        private readonly List<Observation> openObs = new List<Observation>();

        // ---- Output ---------------------------------------------------------
        private string runId;
        private StreamWriter signalsWriter;
        private StreamWriter barsWriter;
        private string instrumentName = "UNKNOWN";
        private string barPeriodStr = "15s";
        private int loggedCount = 0;

        // Horizon minutes at which a direction-normalized close is captured.
        private static readonly int[] HorizonMinutes = new int[] { 1, 3, 5, 10, 15, 30, 60 };

        // ====================================================================
        // USER SETTINGS
        // ====================================================================
        [NinjaScriptProperty]
        [Range(1, 8)]
        [Display(Name = "Min |Score| To Log", Order = 1, GroupName = "1. Collection")]
        public int MinAbsScore { get; set; }

        [NinjaScriptProperty]
        [Range(1, 8)]
        [Display(Name = "Max |Score| To Log", Order = 2, GroupName = "1. Collection")]
        public int MaxAbsScore { get; set; }

        [NinjaScriptProperty]
        [Range(1, 600)]
        [Display(Name = "Forward Window Minutes", Order = 3, GroupName = "1. Collection")]
        public int WindowMinutes { get; set; }

        [NinjaScriptProperty]
        [Display(Name = "Cut Window At RTH Close (right-censor)", Order = 4, GroupName = "1. Collection")]
        public bool CutAtRthClose { get; set; }

        [NinjaScriptProperty]
        [Display(Name = "Log All Sessions (tag RTH/Overnight)", Order = 5, GroupName = "1. Collection")]
        public bool LogAllSessions { get; set; }

        [NinjaScriptProperty]
        [Range(0, 2359)]
        [Display(Name = "RTH Start HHmm", Order = 6, GroupName = "1. Collection")]
        public int RthStart { get; set; }

        [NinjaScriptProperty]
        [Range(0, 2359)]
        [Display(Name = "RTH End HHmm", Order = 7, GroupName = "1. Collection")]
        public int RthEnd { get; set; }

        [Display(Name = "Output Folder", Order = 1, GroupName = "2. Output")]
        public string OutputFolder { get; set; }

        [NinjaScriptProperty]
        [Display(Name = "Write Bars File", Order = 2, GroupName = "2. Output")]
        public bool WriteBarsFile { get; set; }

        [NinjaScriptProperty]
        [Display(Name = "Show Markers On Chart", Order = 3, GroupName = "2. Output")]
        public bool ShowMarkers { get; set; }

        // ---- Composite-filter configuration (which filters count toward the
        //      entry_filters_passed composite). Individual filter verdicts are
        //      ALWAYS logged regardless of these toggles. Defaults mirror the
        //      live MLLorentzianClassification stack. --------------------------
        [NinjaScriptProperty]
        [Display(Name = "Composite Uses Volatility", Order = 1, GroupName = "3. Composite Filter Config")]
        public bool CompUseVolatility { get; set; }

        [NinjaScriptProperty]
        [Display(Name = "Composite Uses Regime", Order = 2, GroupName = "3. Composite Filter Config")]
        public bool CompUseRegime { get; set; }

        [NinjaScriptProperty]
        [Range(-10.0, 10.0)]
        [Display(Name = "Regime Threshold", Order = 3, GroupName = "3. Composite Filter Config")]
        public double RegimeThreshold { get; set; }

        [NinjaScriptProperty]
        [Display(Name = "Composite Uses ADX", Order = 4, GroupName = "3. Composite Filter Config")]
        public bool CompUseAdx { get; set; }

        [NinjaScriptProperty]
        [Range(0, 100)]
        [Display(Name = "ADX Threshold", Order = 5, GroupName = "3. Composite Filter Config")]
        public int AdxThreshold { get; set; }

        [NinjaScriptProperty]
        [Display(Name = "Composite Uses 200 EMA", Order = 6, GroupName = "3. Composite Filter Config")]
        public bool CompUseEma200 { get; set; }

        [NinjaScriptProperty]
        [Display(Name = "Composite Uses 800 EMA", Order = 7, GroupName = "3. Composite Filter Config")]
        public bool CompUseEma800 { get; set; }

        [NinjaScriptProperty]
        [Display(Name = "Composite Uses 200 SMA", Order = 8, GroupName = "3. Composite Filter Config")]
        public bool CompUseSma200 { get; set; }

        [NinjaScriptProperty]
        [Display(Name = "Composite Uses Kernel", Order = 9, GroupName = "3. Composite Filter Config")]
        public bool CompUseKernel { get; set; }

        [NinjaScriptProperty]
        [Display(Name = "Kernel Smoothing", Order = 10, GroupName = "3. Composite Filter Config")]
        public bool KernelSmoothing { get; set; }

        [NinjaScriptProperty]
        [Range(3, 500)]
        [Display(Name = "Kernel Lookback (h)", Order = 11, GroupName = "3. Composite Filter Config")]
        public int KernelH { get; set; }

        [NinjaScriptProperty]
        [Display(Name = "Kernel Relative Weight (r)", Order = 12, GroupName = "3. Composite Filter Config")]
        public double KernelR { get; set; }

        [NinjaScriptProperty]
        [Display(Name = "Kernel Regression Level (x)", Order = 13, GroupName = "3. Composite Filter Config")]
        public int KernelX { get; set; }

        [NinjaScriptProperty]
        [Range(1, 2)]
        [Display(Name = "Kernel Lag", Order = 14, GroupName = "3. Composite Filter Config")]
        public int KernelLag { get; set; }

        [NinjaScriptProperty]
        [Range(1, 100)]
        [Display(Name = "ML Neighbors Count", Order = 1, GroupName = "4. ML Source")]
        public int NeighborsCount { get; set; }

        [NinjaScriptProperty]
        [Range(100, 1000000)]
        [Display(Name = "ML Max Bars Back", Order = 2, GroupName = "4. ML Source")]
        public int MaxBarsBack { get; set; }

        // ====================================================================
        protected override void OnStateChange()
        {
            if (State == State.SetDefaults)
            {
                Description = "v3 observational feature/label logger. Logs raw excursions + point-in-time snapshot for every +/-4..8 Lorentzian signal. No orders.";
                Name = "SignalLogger";
                Calculate = Calculate.OnBarClose;   // one deterministic evaluation per closed bar
                IsOverlay = true;
                DisplayInDataBox = false;
                DrawOnPricePanel = true;
                IsSuspendedWhileInactive = false;

                MinAbsScore = 4;
                MaxAbsScore = 8;
                WindowMinutes = 60;
                CutAtRthClose = true;
                LogAllSessions = true;   // decision 6: log everything, tag it
                RthStart = 930;
                RthEnd = 1600;

                OutputFolder = @"C:\nqbotv3\data\training";
                WriteBarsFile = true;
                ShowMarkers = false;

                // Composite defaults mirror the live classifier's filter stack.
                CompUseVolatility = true;
                CompUseRegime = true;
                RegimeThreshold = -0.1;
                CompUseAdx = true;
                AdxThreshold = 20;
                CompUseEma200 = true;
                CompUseEma800 = true;
                CompUseSma200 = true;
                CompUseKernel = true;
                KernelSmoothing = false;
                KernelH = 8;
                KernelR = 8.0;
                KernelX = 25;
                KernelLag = 2;

                NeighborsCount = 8;
                MaxBarsBack = 2000;
            }
            else if (State == State.Configure)
            {
                // 1-tick secondary series -> accurate MFE/MAE + sequence resolution.
                AddDataSeries(BarsPeriodType.Tick, 1);
            }
            else if (State == State.DataLoaded)
            {
                // Score source: SAME constructor args as the live classifier so the
                // logged score is identical to what fires live.
                ml = MLLorentzianClassification(
                    NeighborsCount, MaxBarsBack, 5, 1,
                    false, false, false,
                    "RSI", 14, 1,
                    "WT", 10, 11,
                    "CCI", 20, 1,
                    "ADX", 20, 2,
                    "RSI", 9, 1,
                    true, true, -0.1,
                    true, 20,
                    true, 200,
                    true, 800,
                    true, 200,
                    true, true, false,
                    8, 8.0, 25, 2,
                    8.0, -8.0,
                    830, 1500,
                    false, false);

                atrRecent     = ATR(1);
                atrHistorical = ATR(10);
                adx           = ADX(14);
                rsi           = RSI(14, 1);
                cci           = CCI(20);
                ema200        = EMA(200);
                ema800        = EMA(800);
                sma200        = SMA(200);
                volAvg        = SMA(VOL(), 20);
                regime        = new RegimeFilterState();

                OpenWriters();
            }
            else if (State == State.Terminated)
            {
                // Flush any observations whose window never completed -> right-censored.
                FinalizeAllOpen("terminated");
                CloseWriters();
            }
        }

        // ====================================================================
        protected override void OnBarUpdate()
        {
            // ---- 1-tick stream: advance every open observation ---------------
            if (BarsInProgress == 1)
            {
                if (CurrentBars[1] < 0) return;
                UpdateOpenObservations(Closes[1][0], Times[1][0]);
                return;
            }

            if (BarsInProgress != 0) return;

            // Log the 15s bar once (path reconstruction file). Done before the
            // warmup guard so the bars file is complete even during ML warmup.
            WriteBarRow();

            if (CurrentBar < Math.Max(820, MaxBarsBack) - 1) { previousBarScore = 0; return; }

            // Keep regime + kernel recurrences advancing every bar.
            double hlc4 = (Open[0] + High[0] + Low[0] + Close[0]) / 4.0;
            regime.Update(hlc4, High[0], Low[0]);

            Func<int, double> src = i => Close[Math.Min(i, CurrentBar)];
            double kernel  = KernelFunctions.RationalQuadratic(src, CurrentBar, KernelH, KernelR, KernelX);
            double kernelG = KernelFunctions.Gaussian(src, CurrentBar, Math.Max(1, KernelH - KernelLag), KernelX);

            UpdateCandleRun();

            int score = (int)Math.Round(ml.Prediction[0]);
            bool qualifies = Math.Abs(score) >= MinAbsScore && Math.Abs(score) <= MaxAbsScore;
            int dir = score > 0 ? 1 : score < 0 ? -1 : 0;

            // ---- Cluster state machine --------------------------------------
            // Update BEFORE building the snapshot so cluster fields are correct.
            bool isFirstInCluster = false;
            if (qualifies)
            {
                if (currentClusterDir == dir)
                {
                    // same-direction continuation
                }
                else
                {
                    // neutral gap or opposite flip already reset the cluster below;
                    // start a new one here.
                    clusterCounter++;
                    currentClusterId = clusterCounter;
                    currentClusterDir = dir;
                    clusterStartBar = CurrentBar;
                    clusterMaxAbsScore = 0;
                    isFirstInCluster = true;
                }
            }

            if (qualifies)
            {
                bool isNewExtreme = Math.Abs(score) > clusterMaxAbsScore;

                int barsSinceClusterStart = CurrentBar - clusterStartBar;
                int lastSameDirBar = dir > 0 ? lastLongSignalBar : lastShortSignalBar;
                int barsSinceLastSameDir = lastSameDirBar < 0 ? -1 : CurrentBar - lastSameDirBar;

                // exact-score sequence position (identical exact scores in a row)
                if (score == exactScoreRunScore) exactScoreRunPos++;
                else { exactScoreRunScore = score; exactScoreRunPos = 1; }

                Observation o = BuildObservation(
                    score, dir, kernel, kernelG,
                    isFirstInCluster, isNewExtreme,
                    barsSinceClusterStart, barsSinceLastSameDir);

                openObs.Add(o);
                loggedCount++;

                if (isNewExtreme) clusterMaxAbsScore = Math.Abs(score);
                if (dir > 0) lastLongSignalBar = CurrentBar; else lastShortSignalBar = CurrentBar;

                if (ShowMarkers)
                {
                    double y = dir > 0 ? Low[0] - TickSize * 4 : High[0] + TickSize * 4;
                    Draw.Text(this, "sig_" + o.SignalId, score.ToString("+#;-#;0"), 0, y,
                        dir > 0 ? Brushes.LimeGreen : Brushes.OrangeRed);
                }
            }
            else
            {
                // Non-qualifying bar. Neutral (-3..+3) or below MinAbsScore ends any
                // active cluster; an opposite qualifying score is handled above.
                currentClusterDir = 0;
                exactScoreRunScore = 0;
                exactScoreRunPos = 0;
            }

            // kernel history recurrence
            kernelPrev2 = kernelPrev1;
            kernelPrev1 = kernel;
            previousAdxValue = adx[0];
            previousBarScore = score;
        }

        // ====================================================================
        // OBSERVATION SNAPSHOT (point-in-time, taken at signal-bar close)
        // Everything here MUST be knowable at Close[0]. No lookahead. (doc 6)
        // ====================================================================
        private Observation BuildObservation(
            int score, int dir, double kernel, double kernelG,
            bool isFirstInCluster, bool isNewExtreme,
            int barsSinceClusterStart, int barsSinceLastSameDir)
        {
            bool isLong = dir > 0;
            double refPrice = Close[0];          // decision 4: signal-bar close
            DateTime t = Time[0];

            Observation o = new Observation();

            // -- identity (decision 7) --
            o.RunId = runId;
            o.Instrument = instrumentName;
            o.BarPeriod = barPeriodStr;
            o.SignalTime = t;
            o.SessionDate = SessionDateFor(t);
            o.Score = score;
            o.Direction = isLong ? "LONG" : "SHORT";
            o.SequencePosition = exactScoreRunPos;
            o.SignalId = string.Format(CultureInfo.InvariantCulture,
                "{0}|{1}|{2:yyyyMMddHHmmss}|{3}|{4:+#;-#;0}|seq{5}",
                instrumentName, barPeriodStr, t, isLong ? "L" : "S", score, exactScoreRunPos);

            // -- cluster (decision 1) --
            o.ClusterId = currentClusterId;
            o.IsFirstInCluster = isFirstInCluster;
            o.BarsSinceClusterStart = barsSinceClusterStart;
            o.BarsSinceLastSameDir = barsSinceLastSameDir;
            o.PreviousBarScore = previousBarScore;
            o.ScoreChangedFromPrior = (score != previousBarScore);
            o.IsNewScoreExtreme = isNewExtreme;

            // -- reference & session --
            o.RefPrice = refPrice;
            o.SessionTag = IsInSession(t, RthStart, RthEnd) ? "RTH" : "OVERNIGHT";

            // -- filters: individual verdict + raw value (decision 2) ----------
            // Each verdict is computed UNCONDITIONALLY (as if the filter were on)
            // so its marginal value can be measured later even when off.
            o.AtrRecent = Safe(atrRecent[0]);
            o.AtrHistorical = Safe(atrHistorical[0]);
            o.AtrRatio = (atrHistorical[0] > 0) ? Safe(atrRecent[0] / atrHistorical[0]) : double.NaN;
            o.VolatilityPass = MLExtensions.FilterVolatility(atrRecent[0], atrHistorical[0], true);

            o.RegimeValue = SafeRegimeValue();
            o.RegimePass = regime.Evaluate(RegimeThreshold, true);

            o.AdxValue = Safe(adx[0]);
            o.AdxPass = MLExtensions.FilterAdx(adx[0], AdxThreshold, true);

            o.Ema200 = Safe(ema200[0]);
            o.DistEma200Ticks = (refPrice - ema200[0]) / TickSize;       // signed
            o.Ema200Pass = isLong ? refPrice > ema200[0] : refPrice < ema200[0];

            o.Ema800 = Safe(ema800[0]);
            o.DistEma800Ticks = (refPrice - ema800[0]) / TickSize;
            o.Ema800Pass = isLong ? refPrice > ema800[0] : refPrice < ema800[0];

            o.Sma200 = Safe(sma200[0]);
            o.DistSma200Ticks = (refPrice - sma200[0]) / TickSize;
            o.Sma200Pass = isLong ? refPrice > sma200[0] : refPrice < sma200[0];

            o.KernelValue = Safe(kernel);
            o.KernelGaussian = Safe(kernelG);
            o.KernelSlopeTicks = double.IsNaN(kernelPrev1) ? double.NaN : (kernel - kernelPrev1) / TickSize;
            o.PriceMinusKernelTicks = (refPrice - kernel) / TickSize;
            o.KernelPass = KernelVerdict(isLong, kernel, kernelG);

            o.SessionPass = IsInSession(t, RthStart, RthEnd);

            // -- composite (decision 2/3): respects the Comp* toggles ----------
            o.EntryFiltersPassed = CompositeFiltersPassed(o);
            o.FilterConfig = ActiveCompositeConfig();

            // -- execution states kept SEPARATE (decision 3) -------------------
            // This logger is observational; it never executes. Live wiring / a
            // separate reconciliation step populates these. Defaults are explicit.
            o.CandidateExecutedLive = false;
            o.ManualIntervention = false;

            // -- extra point-in-time features (doc 6) --------------------------
            o.Rsi = Safe(rsi[0]);
            o.Cci = Safe(cci[0]);
            double atrTicks = Math.Max(1.0, atrRecent[0] / TickSize);
            o.DistEma200Atr = (refPrice - ema200[0]) / TickSize / atrTicks;
            o.DistEma800Atr = (refPrice - ema800[0]) / TickSize / atrTicks;
            o.DistVwapTicks = double.IsNaN(CurrentVwap()) ? double.NaN : (refPrice - CurrentVwap()) / TickSize;
            o.MinutesSinceRthOpen = MinutesSinceRthOpen(t);
            o.MinuteOfDay = t.Hour * 60 + t.Minute;
            o.DayOfWeek = t.DayOfWeek.ToString();
            o.CandleRun = sameDirectionCandleRun;
            double range = Math.Max(TickSize, High[0] - Low[0]);
            o.BodyToRange = Math.Abs(Close[0] - Open[0]) / range;
            o.Volume = Volume[0];
            o.VolumeRatio = (volAvg[0] > 0) ? Safe(Volume[0] / volAvg[0]) : double.NaN;
            o.BarRangeTicks = (High[0] - Low[0]) / TickSize;

            // -- label window setup (decision 5) -------------------------------
            o.IsLong = isLong;
            o.HorizonDeltaTicks = new double[HorizonMinutes.Length];
            for (int i = 0; i < HorizonMinutes.Length; i++) o.HorizonDeltaTicks[i] = double.NaN;
            o.WindowEnd = t.AddMinutes(WindowMinutes);
            if (CutAtRthClose && o.SessionTag == "RTH")
            {
                DateTime rthCloseDt = RthCloseFor(t);
                if (rthCloseDt < o.WindowEnd) { o.WindowEnd = rthCloseDt; o.CutByRthClose = true; }
            }
            o.MfeTicks = 0;
            o.MaeTicks = 0;
            o.MaeBeforeMfe = false;
            o.SeenAnyTick = false;

            return o;
        }

        // ====================================================================
        // FORWARD TRACKING on the 1-tick stream (decision 5)
        // ====================================================================
        private void UpdateOpenObservations(double price, DateTime tickTime)
        {
            if (openObs.Count == 0) return;

            for (int i = openObs.Count - 1; i >= 0; i--)
            {
                Observation o = openObs[i];
                if (tickTime < o.SignalTime) continue;   // never resolve on data before entry

                double favTicks = o.IsLong ? (price - o.RefPrice) / TickSize : (o.RefPrice - price) / TickSize;
                double advTicks = o.IsLong ? (o.RefPrice - price) / TickSize : (price - o.RefPrice) / TickSize;

                if (favTicks > o.MfeTicks)
                {
                    o.MfeTicks = favTicks;
                    o.MinutesToMfe = (tickTime - o.SignalTime).TotalMinutes;
                    if (!o.SeenMae) o.MaeBeforeMfe = false; // MFE reached w/o a prior recorded MAE extreme
                }
                if (advTicks > o.MaeTicks)
                {
                    o.MaeTicks = advTicks;
                    o.MinutesToMae = (tickTime - o.SignalTime).TotalMinutes;
                    o.SeenMae = true;
                    if (o.MfeTicks <= 0) o.MaeBeforeMfe = true; // adverse extreme before any favorable
                }
                o.SeenAnyTick = true;

                // horizon captures (first tick at/after each boundary)
                for (int h = 0; h < HorizonMinutes.Length; h++)
                {
                    if (double.IsNaN(o.HorizonDeltaTicks[h]) &&
                        tickTime >= o.SignalTime.AddMinutes(HorizonMinutes[h]))
                    {
                        o.HorizonDeltaTicks[h] = o.IsLong
                            ? (price - o.RefPrice) / TickSize
                            : (o.RefPrice - price) / TickSize;
                    }
                }

                if (tickTime >= o.WindowEnd)
                {
                    o.FinalDeltaTicks = o.IsLong ? (price - o.RefPrice) / TickSize : (o.RefPrice - price) / TickSize;
                    o.RightCensored = o.CutByRthClose; // window truncated by RTH close
                    FinalizeObservation(o, "window_complete");
                    openObs.RemoveAt(i);
                }
            }
        }

        // ====================================================================
        // FILTER HELPERS
        // ====================================================================
        private bool KernelVerdict(bool isLong, double kernel, double kernelG)
        {
            bool bullish, bearish;
            if (KernelSmoothing)
            {
                bullish = kernelG >= kernel;
                bearish = kernelG <= kernel;
            }
            else
            {
                bullish = !double.IsNaN(kernelPrev1) && kernelPrev1 < kernel;
                bearish = !double.IsNaN(kernelPrev1) && kernelPrev1 > kernel;
            }
            return isLong ? bullish : bearish;
        }

        private bool CompositeFiltersPassed(Observation o)
        {
            if (CompUseVolatility && !o.VolatilityPass) return false;
            if (CompUseRegime && !o.RegimePass) return false;
            if (CompUseAdx && !o.AdxPass) return false;
            if (CompUseEma200 && !o.Ema200Pass) return false;
            if (CompUseEma800 && !o.Ema800Pass) return false;
            if (CompUseSma200 && !o.Sma200Pass) return false;
            if (CompUseKernel && !o.KernelPass) return false;
            return true;
        }

        private string ActiveCompositeConfig()
        {
            List<string> parts = new List<string>();
            if (CompUseVolatility) parts.Add("vol");
            if (CompUseRegime) parts.Add("regime");
            if (CompUseAdx) parts.Add("adx");
            if (CompUseEma200) parts.Add("ema200");
            if (CompUseEma800) parts.Add("ema800");
            if (CompUseSma200) parts.Add("sma200");
            if (CompUseKernel) parts.Add("kernel");
            return parts.Count == 0 ? "none" : string.Join("+", parts);
        }

        private double SafeRegimeValue()
        {
            try { return Safe(regime.NormalizedSlopeDecline); }
            catch { return double.NaN; }
        }

        // ====================================================================
        // SESSION / TIME HELPERS
        // ====================================================================
        private bool IsInSession(DateTime t, int startHHmm, int endHHmm)
        {
            int hhmm = t.Hour * 100 + t.Minute;
            if (startHHmm <= endHHmm) return hhmm >= startHHmm && hhmm <= endHHmm;
            return hhmm >= startHHmm || hhmm <= endHHmm;
        }

        private DateTime RthCloseFor(DateTime t)
        {
            int h = RthEnd / 100, m = RthEnd % 100;
            return new DateTime(t.Year, t.Month, t.Day, h, m, 0);
        }

        private string SessionDateFor(DateTime t)
        {
            // Trading-day date. Overnight bars before RTH open still belong to the
            // calendar date here; refine to a session-boundary rule later if needed.
            return t.ToString("yyyy-MM-dd", CultureInfo.InvariantCulture);
        }

        private double MinutesSinceRthOpen(DateTime t)
        {
            int h = RthStart / 100, m = RthStart % 100;
            DateTime open = new DateTime(t.Year, t.Month, t.Day, h, m, 0);
            return (t - open).TotalMinutes;
        }

        // ---- Session VWAP (reset at RTH open) -------------------------------
        private void UpdateVwap()
        {
            DateTime t = Time[0];
            string day = t.ToString("yyyy-MM-dd");
            bool rthOpenReached = IsInSession(t, RthStart, RthEnd);

            if (vwapSessionDate.ToString("yyyy-MM-dd") != day && rthOpenReached)
            {
                vwapSessionDate = t.Date;
                vwapCumPV = 0.0;
                vwapCumV = 0.0;
            }
            if (rthOpenReached && vwapSessionDate != DateTime.MinValue)
            {
                double tp = (High[0] + Low[0] + Close[0]) / 3.0;
                double v = Volume[0];
                vwapCumPV += tp * v;
                vwapCumV += v;
            }
        }

        private double CurrentVwap()
        {
            return vwapCumV > 0 ? vwapCumPV / vwapCumV : double.NaN;
        }

        private void UpdateCandleRun()
        {
            int d = Close[0] > Open[0] ? 1 : Close[0] < Open[0] ? -1 : 0;
            if (d != 0 && d == previousCandleDirection) sameDirectionCandleRun++;
            else if (d != 0) sameDirectionCandleRun = 1;
            else sameDirectionCandleRun = 0;
            previousCandleDirection = d;

            UpdateVwap();
        }

        // ====================================================================
        // OUTPUT
        // ====================================================================
        private void OpenWriters()
        {
            try
            {
                instrumentName = Instrument != null && Instrument.MasterInstrument != null
                    ? Instrument.MasterInstrument.Name : "UNKNOWN";
                barPeriodStr = string.Format(CultureInfo.InvariantCulture, "{0}{1}",
                    BarsPeriod.Value, BarsPeriod.BarsPeriodType == BarsPeriodType.Second ? "s"
                        : BarsPeriod.BarsPeriodType == BarsPeriodType.Minute ? "m" : "?");

                runId = "run_" + DateTime.UtcNow.ToString("yyyyMMddTHHmmssZ", CultureInfo.InvariantCulture)
                        + "_" + Guid.NewGuid().ToString("N").Substring(0, 8);

                Directory.CreateDirectory(OutputFolder);

                string sigPath = Path.Combine(OutputFolder, "signals_" + runId + ".csv");
                signalsWriter = new StreamWriter(sigPath, false, Encoding.UTF8) { AutoFlush = true };
                signalsWriter.WriteLine(SignalHeader());

                if (WriteBarsFile)
                {
                    string barPath = Path.Combine(OutputFolder, "bars_" + runId + ".csv");
                    barsWriter = new StreamWriter(barPath, false, Encoding.UTF8) { AutoFlush = true };
                    barsWriter.WriteLine("run_id,instrument,bar_period,bar_time,session_date,session_tag,open,high,low,close,volume");
                }

                Print("SignalLogger: writing " + sigPath);
            }
            catch (Exception ex)
            {
                Print("SignalLogger: FAILED to open writers: " + ex.Message);
            }
        }

        private void WriteBarRow()
        {
            if (barsWriter == null || !WriteBarsFile) return;
            try
            {
                DateTime t = Time[0];
                barsWriter.WriteLine(string.Join(",",
                    runId,
                    instrumentName,
                    barPeriodStr,
                    t.ToString("yyyy-MM-ddTHH:mm:ss", CultureInfo.InvariantCulture),
                    SessionDateFor(t),
                    IsInSession(t, RthStart, RthEnd) ? "RTH" : "OVERNIGHT",
                    F(Open[0]), F(High[0]), F(Low[0]), F(Close[0]), F(Volume[0])));
            }
            catch { /* never let logging kill the indicator */ }
        }

        private void FinalizeObservation(Observation o, string reason)
        {
            if (signalsWriter == null) return;
            try { signalsWriter.WriteLine(BuildSignalRow(o, reason)); }
            catch (Exception ex) { Print("SignalLogger: row write failed: " + ex.Message); }
        }

        private void FinalizeAllOpen(string reason)
        {
            for (int i = openObs.Count - 1; i >= 0; i--)
            {
                Observation o = openObs[i];
                o.RightCensored = true; // window never completed
                FinalizeObservation(o, reason);
            }
            openObs.Clear();
        }

        private void CloseWriters()
        {
            try { if (signalsWriter != null) { signalsWriter.Flush(); signalsWriter.Close(); signalsWriter = null; } } catch { }
            try { if (barsWriter != null) { barsWriter.Flush(); barsWriter.Close(); barsWriter = null; } } catch { }
        }

        private string SignalHeader()
        {
            StringBuilder sb = new StringBuilder();
            sb.Append("signal_id,run_id,instrument,bar_period,signal_time,session_date,session_tag,");
            sb.Append("score,direction,is_long,exact_score_sequence_position,");
            sb.Append("signal_cluster_id,is_first_signal_in_cluster,bars_since_cluster_start,bars_since_last_same_dir_signal,");
            sb.Append("previous_prediction_score,score_changed_from_prior_bar,is_new_score_extreme,");
            sb.Append("signal_reference_price,");
            // filters (pass + raw)
            sb.Append("volatility_pass,atr_recent,atr_historical,atr_ratio,");
            sb.Append("regime_pass,regime_value,");
            sb.Append("adx_pass,adx_value,");
            sb.Append("ema200_pass,ema200_value,dist_ema200_ticks,dist_ema200_atr,");
            sb.Append("ema800_pass,ema800_value,dist_ema800_ticks,dist_ema800_atr,");
            sb.Append("sma200_pass,sma200_value,dist_sma200_ticks,");
            sb.Append("kernel_pass,kernel_value,kernel_gaussian,kernel_slope_ticks,price_minus_kernel_ticks,");
            sb.Append("session_pass,entry_filters_passed,filter_config,");
            // execution states (separate)
            sb.Append("candidate_executed_live,manual_intervention,");
            // extra features
            sb.Append("rsi,cci,dist_vwap_ticks,minutes_since_rth_open,minute_of_day,day_of_week,");
            sb.Append("candle_run,body_to_range,volume,volume_ratio,bar_range_ticks,");
            // labels
            sb.Append("mfe_ticks,mae_ticks,minutes_to_mfe,minutes_to_mae,mae_before_mfe,");
            sb.Append("delta_1m,delta_3m,delta_5m,delta_10m,delta_15m,delta_30m,delta_60m,");
            sb.Append("final_delta_ticks,window_end,right_censored,window_minutes,finalize_reason");
            return sb.ToString();
        }

        private string BuildSignalRow(Observation o, string reason)
        {
            StringBuilder sb = new StringBuilder();
            Add(sb, Q(o.SignalId));
            Add(sb, o.RunId);
            Add(sb, o.Instrument);
            Add(sb, o.BarPeriod);
            Add(sb, o.SignalTime.ToString("yyyy-MM-ddTHH:mm:ss", CultureInfo.InvariantCulture));
            Add(sb, o.SessionDate);
            Add(sb, o.SessionTag);
            Add(sb, o.Score.ToString(CultureInfo.InvariantCulture));
            Add(sb, o.Direction);
            Add(sb, o.IsLong ? "1" : "0");
            Add(sb, o.SequencePosition.ToString(CultureInfo.InvariantCulture));
            Add(sb, o.ClusterId.ToString(CultureInfo.InvariantCulture));
            Add(sb, o.IsFirstInCluster ? "1" : "0");
            Add(sb, o.BarsSinceClusterStart.ToString(CultureInfo.InvariantCulture));
            Add(sb, o.BarsSinceLastSameDir.ToString(CultureInfo.InvariantCulture));
            Add(sb, o.PreviousBarScore.ToString(CultureInfo.InvariantCulture));
            Add(sb, o.ScoreChangedFromPrior ? "1" : "0");
            Add(sb, o.IsNewScoreExtreme ? "1" : "0");
            Add(sb, F(o.RefPrice));
            Add(sb, B(o.VolatilityPass)); Add(sb, F(o.AtrRecent)); Add(sb, F(o.AtrHistorical)); Add(sb, F(o.AtrRatio));
            Add(sb, B(o.RegimePass)); Add(sb, F(o.RegimeValue));
            Add(sb, B(o.AdxPass)); Add(sb, F(o.AdxValue));
            Add(sb, B(o.Ema200Pass)); Add(sb, F(o.Ema200)); Add(sb, F(o.DistEma200Ticks)); Add(sb, F(o.DistEma200Atr));
            Add(sb, B(o.Ema800Pass)); Add(sb, F(o.Ema800)); Add(sb, F(o.DistEma800Ticks)); Add(sb, F(o.DistEma800Atr));
            Add(sb, B(o.Sma200Pass)); Add(sb, F(o.Sma200)); Add(sb, F(o.DistSma200Ticks));
            Add(sb, B(o.KernelPass)); Add(sb, F(o.KernelValue)); Add(sb, F(o.KernelGaussian)); Add(sb, F(o.KernelSlopeTicks)); Add(sb, F(o.PriceMinusKernelTicks));
            Add(sb, B(o.SessionPass)); Add(sb, B(o.EntryFiltersPassed)); Add(sb, o.FilterConfig);
            Add(sb, o.CandidateExecutedLive ? "1" : "0"); Add(sb, o.ManualIntervention ? "1" : "0");
            Add(sb, F(o.Rsi)); Add(sb, F(o.Cci)); Add(sb, F(o.DistVwapTicks));
            Add(sb, F(o.MinutesSinceRthOpen)); Add(sb, o.MinuteOfDay.ToString(CultureInfo.InvariantCulture)); Add(sb, o.DayOfWeek);
            Add(sb, o.CandleRun.ToString(CultureInfo.InvariantCulture)); Add(sb, F(o.BodyToRange)); Add(sb, F(o.Volume)); Add(sb, F(o.VolumeRatio)); Add(sb, F(o.BarRangeTicks));
            Add(sb, F(o.MfeTicks)); Add(sb, F(o.MaeTicks)); Add(sb, F(o.MinutesToMfe)); Add(sb, F(o.MinutesToMae)); Add(sb, o.MaeBeforeMfe ? "1" : "0");
            for (int i = 0; i < o.HorizonDeltaTicks.Length; i++) Add(sb, F(o.HorizonDeltaTicks[i]));
            Add(sb, F(o.FinalDeltaTicks));
            Add(sb, o.WindowEnd.ToString("yyyy-MM-ddTHH:mm:ss", CultureInfo.InvariantCulture));
            Add(sb, o.RightCensored ? "1" : "0");
            Add(sb, WindowMinutes.ToString(CultureInfo.InvariantCulture));
            sb.Append(reason); // last column, no trailing comma
            return sb.ToString();
        }

        // ---- tiny formatting helpers ----------------------------------------
        private static void Add(StringBuilder sb, string v) { sb.Append(v); sb.Append(','); }
        private static string B(bool b) { return b ? "1" : "0"; }
        private static double Safe(double v) { return (double.IsNaN(v) || double.IsInfinity(v)) ? double.NaN : v; }
        private static string F(double v)
        {
            if (double.IsNaN(v) || double.IsInfinity(v)) return "";
            return v.ToString("0.########", CultureInfo.InvariantCulture);
        }
        private static string Q(string s)
        {
            if (string.IsNullOrEmpty(s)) return "";
            if (s.IndexOf(',') >= 0 || s.IndexOf('"') >= 0)
                return "\"" + s.Replace("\"", "\"\"") + "\"";
            return s;
        }

        // ====================================================================
        // Observation record (one per qualifying bar; label fields filled forward)
        // ====================================================================
        private class Observation
        {
            // identity
            public string SignalId, RunId, Instrument, BarPeriod, Direction, SessionDate, SessionTag;
            public DateTime SignalTime;
            public int Score, SequencePosition;
            public bool IsLong;
            // cluster
            public int ClusterId, BarsSinceClusterStart, BarsSinceLastSameDir, PreviousBarScore;
            public bool IsFirstInCluster, ScoreChangedFromPrior, IsNewScoreExtreme;
            // reference
            public double RefPrice;
            // filters
            public bool VolatilityPass, RegimePass, AdxPass, Ema200Pass, Ema800Pass, Sma200Pass, KernelPass, SessionPass, EntryFiltersPassed;
            public double AtrRecent, AtrHistorical, AtrRatio, RegimeValue, AdxValue;
            public double Ema200, Ema800, Sma200, DistEma200Ticks, DistEma800Ticks, DistSma200Ticks, DistEma200Atr, DistEma800Atr;
            public double KernelValue, KernelGaussian, KernelSlopeTicks, PriceMinusKernelTicks;
            public string FilterConfig;
            // execution states
            public bool CandidateExecutedLive, ManualIntervention;
            // features
            public double Rsi, Cci, DistVwapTicks, MinutesSinceRthOpen, BodyToRange, Volume, VolumeRatio, BarRangeTicks;
            public int MinuteOfDay, CandleRun;
            public string DayOfWeek;
            // labels
            public double MfeTicks, MaeTicks, MinutesToMfe = double.NaN, MinutesToMae = double.NaN, FinalDeltaTicks = double.NaN;
            public double[] HorizonDeltaTicks;
            public bool MaeBeforeMfe, SeenMae, SeenAnyTick, RightCensored, CutByRthClose;
            public DateTime WindowEnd;
        }
    }
}

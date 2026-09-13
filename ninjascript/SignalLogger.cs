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
// NQ BOT v3 - SignalLogger  (rev 2 - data-integrity review fixes)
// ----------------------------------------------------------------------------
// Purely observational research indicator. It NEVER submits orders.
// Logs raw excursions + a point-in-time feature snapshot for every qualifying
// Lorentzian signal, so any exit target can be derived post-hoc.
//
// Review fixes in this revision:
//  1. Forward-window boundary: a tick strictly after WindowEnd can no longer
//     affect MFE/MAE/horizons/final. Finalize uses the last in-window price.
//  2. mae_before_mfe = time_of_final_MAE < time_of_final_MFE, computed at
//     finalize; a non-occurring excursion counts as +infinity (never happened).
//  3. VWAP / regime / candle-run / kernel advance on EVERY bar; the ML warmup
//     guard only gates SIGNAL creation, not stateful feature history.
//  4. Contract-aware identity: full contract, master, expiry, tick size stored;
//     signal_id uses the full contract so different contracts never collide.
//  6. LogAllSessions now actually gates observation creation.
//  7/11. Censored metadata: window_end_scheduled, window_end_actual, final_price,
//     right_censored, and a distinct finalize_reason (rth_close vs window_complete
//     vs terminated).
//  Run metadata (<run_id>.meta.json) records instrument, tick size, timezone,
//  RTH + classifier session config, and collection settings for reproducibility.
//
// Apply to a 15-second NQ/MNQ chart. A 1-tick secondary series drives forward
// tracking. Timezone note: Time[0], RthStart and RthEnd are all in the chart's
// trading-hours timezone (recorded as timezone_id in the run metadata). The
// classifier's internal session (830-1500) is passed through unchanged from the
// live config and also recorded, so any ET/CT offset is auditable, not silent.
// ============================================================================

namespace NinjaTrader.NinjaScript.Indicators
{
    public class SignalLogger : Indicator
    {
        private MLLorentzianClassification ml;

        private ATR atrRecent, atrHistorical;
        private ADX adx;
        private RSI rsi;
        private CCI cci;
        private EMA ema200, ema800;
        private SMA sma200, volAvg;
        private RegimeFilterState regime;

        private double kernelPrev1 = double.NaN;
        private double kernelPrev2 = double.NaN;
        private double previousAdxValue = double.NaN;

        private int sameDirectionCandleRun = 0;
        private int previousCandleDirection = 0;
        private int previousBarScore = 0;

        private int clusterCounter = 0;
        private int currentClusterId = 0;
        private int currentClusterDir = 0;
        private int clusterStartBar = -1;
        private int clusterMaxAbsScore = 0;
        private int exactScoreRunScore = 0;
        private int exactScoreRunPos = 0;
        private int lastLongSignalBar = -1;
        private int lastShortSignalBar = -1;

        private DateTime vwapSessionDate = DateTime.MinValue;
        private double vwapCumPV = 0.0;
        private double vwapCumV = 0.0;

        private readonly List<Observation> openObs = new List<Observation>();

        private string runId;
        private StreamWriter signalsWriter;
        private StreamWriter barsWriter;

        // contract-aware identity (review point 4)
        private string instrumentFull = "UNKNOWN";
        private string instrumentMaster = "UNKNOWN";
        private string expiryStr = "";
        private double tickSizeVal = 0.25;
        private string timezoneId = "unknown";
        private string barPeriodStr = "15s";
        private int loggedCount = 0;

        private static readonly int[] HorizonMinutes = new int[] { 1, 3, 5, 10, 15, 30, 60 };

        // ============================ SETTINGS ==============================
        [NinjaScriptProperty][Range(1, 8)]
        [Display(Name = "Min |Score| To Log", Order = 1, GroupName = "1. Collection")]
        public int MinAbsScore { get; set; }

        [NinjaScriptProperty][Range(1, 8)]
        [Display(Name = "Max |Score| To Log", Order = 2, GroupName = "1. Collection")]
        public int MaxAbsScore { get; set; }

        [NinjaScriptProperty][Range(1, 600)]
        [Display(Name = "Forward Window Minutes", Order = 3, GroupName = "1. Collection")]
        public int WindowMinutes { get; set; }

        [NinjaScriptProperty]
        [Display(Name = "Cut Window At RTH Close (right-censor)", Order = 4, GroupName = "1. Collection")]
        public bool CutAtRthClose { get; set; }

        [NinjaScriptProperty]
        [Display(Name = "Log All Sessions (false = RTH only)", Order = 5, GroupName = "1. Collection")]
        public bool LogAllSessions { get; set; }

        [NinjaScriptProperty][Range(0, 2359)]
        [Display(Name = "RTH Start HHmm", Order = 6, GroupName = "1. Collection")]
        public int RthStart { get; set; }

        [NinjaScriptProperty][Range(0, 2359)]
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

        [NinjaScriptProperty]
        [Display(Name = "Composite Uses Volatility", Order = 1, GroupName = "3. Composite Filter Config")]
        public bool CompUseVolatility { get; set; }
        [NinjaScriptProperty]
        [Display(Name = "Composite Uses Regime", Order = 2, GroupName = "3. Composite Filter Config")]
        public bool CompUseRegime { get; set; }
        [NinjaScriptProperty][Range(-10.0, 10.0)]
        [Display(Name = "Regime Threshold", Order = 3, GroupName = "3. Composite Filter Config")]
        public double RegimeThreshold { get; set; }
        [NinjaScriptProperty]
        [Display(Name = "Composite Uses ADX", Order = 4, GroupName = "3. Composite Filter Config")]
        public bool CompUseAdx { get; set; }
        [NinjaScriptProperty][Range(0, 100)]
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
        [NinjaScriptProperty][Range(3, 500)]
        [Display(Name = "Kernel Lookback (h)", Order = 11, GroupName = "3. Composite Filter Config")]
        public int KernelH { get; set; }
        [NinjaScriptProperty]
        [Display(Name = "Kernel Relative Weight (r)", Order = 12, GroupName = "3. Composite Filter Config")]
        public double KernelR { get; set; }
        [NinjaScriptProperty]
        [Display(Name = "Kernel Regression Level (x)", Order = 13, GroupName = "3. Composite Filter Config")]
        public int KernelX { get; set; }
        [NinjaScriptProperty][Range(1, 2)]
        [Display(Name = "Kernel Lag", Order = 14, GroupName = "3. Composite Filter Config")]
        public int KernelLag { get; set; }

        [NinjaScriptProperty][Range(1, 100)]
        [Display(Name = "ML Neighbors Count", Order = 1, GroupName = "4. ML Source")]
        public int NeighborsCount { get; set; }
        [NinjaScriptProperty][Range(100, 1000000)]
        [Display(Name = "ML Max Bars Back", Order = 2, GroupName = "4. ML Source")]
        public int MaxBarsBack { get; set; }

        // ============================ LIFECYCLE ============================
        protected override void OnStateChange()
        {
            if (State == State.SetDefaults)
            {
                Description = "v3 observational feature/label logger (rev 2). No orders.";
                Name = "SignalLogger";
                Calculate = Calculate.OnBarClose;
                IsOverlay = true;
                DisplayInDataBox = false;
                DrawOnPricePanel = true;
                IsSuspendedWhileInactive = false;

                MinAbsScore = 4;
                MaxAbsScore = 8;
                WindowMinutes = 60;
                CutAtRthClose = true;
                LogAllSessions = true;
                RthStart = 930;
                RthEnd = 1600;

                OutputFolder = @"C:\nqbotv3\data\training";
                WriteBarsFile = true;
                ShowMarkers = false;

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
                AddDataSeries(BarsPeriodType.Tick, 1);
            }
            else if (State == State.DataLoaded)
            {
                ml = MLLorentzianClassification(
                    NeighborsCount, MaxBarsBack, 5, 1,
                    false, false, false,
                    "RSI", 14, 1, "WT", 10, 11, "CCI", 20, 1, "ADX", 20, 2, "RSI", 9, 1,
                    true, true, -0.1, true, 20, true, 200, true, 800, true, 200,
                    true, true, false, 8, 8.0, 25, 2, 8.0, -8.0,
                    830, 1500, false, false);

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
                FinalizeAllOpen("terminated");   // incomplete windows -> right-censored
                CloseWriters();
            }
        }

        // ============================ MAIN LOOP ============================
        protected override void OnBarUpdate()
        {
            if (BarsInProgress == 1)
            {
                if (CurrentBars[1] < 0) return;
                UpdateOpenObservations(Closes[1][0], Times[1][0]);
                return;
            }
            if (BarsInProgress != 0) return;

            WriteBarRow();

            // ---- (fix 3) stateful features advance on EVERY bar ----
            double hlc4 = (Open[0] + High[0] + Low[0] + Close[0]) / 4.0;
            regime.Update(hlc4, High[0], Low[0]);
            Func<int, double> src = i => Close[Math.Min(i, CurrentBar)];
            double kernel  = KernelFunctions.RationalQuadratic(src, CurrentBar, KernelH, KernelR, KernelX);
            double kernelG = KernelFunctions.Gaussian(src, CurrentBar, Math.Max(1, KernelH - KernelLag), KernelX);
            UpdateCandleRun();   // candle run + session VWAP, every bar

            // Warmup gates SIGNAL creation only (ML prediction needs its history).
            bool warm = CurrentBar >= Math.Max(820, MaxBarsBack) - 1;
            int score = 0;
            if (warm)
            {
                score = (int)Math.Round(ml.Prediction[0]);
                bool qualifies = Math.Abs(score) >= MinAbsScore && Math.Abs(score) <= MaxAbsScore;
                int dir = score > 0 ? 1 : score < 0 ? -1 : 0;

                bool isFirstInCluster = false;
                if (qualifies && currentClusterDir != dir)
                {
                    clusterCounter++;
                    currentClusterId = clusterCounter;
                    currentClusterDir = dir;
                    clusterStartBar = CurrentBar;
                    clusterMaxAbsScore = 0;
                    isFirstInCluster = true;
                }

                if (qualifies)
                {
                    bool isNewExtreme = Math.Abs(score) > clusterMaxAbsScore;
                    int barsSinceClusterStart = CurrentBar - clusterStartBar;
                    int lastSameDirBar = dir > 0 ? lastLongSignalBar : lastShortSignalBar;
                    int barsSinceLastSameDir = lastSameDirBar < 0 ? -1 : CurrentBar - lastSameDirBar;
                    if (score == exactScoreRunScore) exactScoreRunPos++;
                    else { exactScoreRunScore = score; exactScoreRunPos = 1; }

                    bool inRth = IsInSession(Time[0], RthStart, RthEnd);
                    bool shouldLog = LogAllSessions || inRth;   // (fix 6)

                    if (shouldLog)
                    {
                        Observation o = BuildObservation(score, dir, kernel, kernelG,
                            isFirstInCluster, isNewExtreme, barsSinceClusterStart, barsSinceLastSameDir);
                        openObs.Add(o);
                        loggedCount++;
                        if (ShowMarkers)
                        {
                            double y = dir > 0 ? Low[0] - TickSize * 4 : High[0] + TickSize * 4;
                            Draw.Text(this, "sig_" + o.SignalId, score.ToString("+#;-#;0"), 0, y,
                                dir > 0 ? Brushes.LimeGreen : Brushes.OrangeRed);
                        }
                    }

                    if (isNewExtreme) clusterMaxAbsScore = Math.Abs(score);
                    if (dir > 0) lastLongSignalBar = CurrentBar; else lastShortSignalBar = CurrentBar;
                }
                else
                {
                    currentClusterDir = 0;
                    exactScoreRunScore = 0;
                    exactScoreRunPos = 0;
                }
            }

            // advance recurrences EVERY bar
            kernelPrev2 = kernelPrev1;
            kernelPrev1 = kernel;
            previousAdxValue = adx[0];
            previousBarScore = warm ? score : 0;
        }

        // ===================== OBSERVATION SNAPSHOT ========================
        private Observation BuildObservation(int score, int dir, double kernel, double kernelG,
            bool isFirstInCluster, bool isNewExtreme, int barsSinceClusterStart, int barsSinceLastSameDir)
        {
            bool isLong = dir > 0;
            double refPrice = Close[0];
            DateTime t = Time[0];

            Observation o = new Observation();

            o.RunId = runId;
            o.Instrument = instrumentFull;
            o.InstrumentMaster = instrumentMaster;
            o.Expiry = expiryStr;
            o.TickSize = tickSizeVal;
            o.BarPeriod = barPeriodStr;
            o.SignalTime = t;
            o.SessionDate = SessionDateFor(t);
            o.Score = score;
            o.Direction = isLong ? "LONG" : "SHORT";
            o.SequencePosition = exactScoreRunPos;
            o.SignalId = string.Format(CultureInfo.InvariantCulture,
                "{0}|{1}|{2:yyyyMMddHHmmss}|{3}|{4:+#;-#;0}|seq{5}",
                instrumentFull, barPeriodStr, t, isLong ? "L" : "S", score, exactScoreRunPos);

            o.ClusterId = currentClusterId;
            o.IsFirstInCluster = isFirstInCluster;
            o.BarsSinceClusterStart = barsSinceClusterStart;
            o.BarsSinceLastSameDir = barsSinceLastSameDir;
            o.PreviousBarScore = previousBarScore;
            o.ScoreChangedFromPrior = (score != previousBarScore);
            o.IsNewScoreExtreme = isNewExtreme;

            o.RefPrice = refPrice;
            o.SessionTag = IsInSession(t, RthStart, RthEnd) ? "RTH" : "OVERNIGHT";

            o.AtrRecent = Safe(atrRecent[0]);
            o.AtrHistorical = Safe(atrHistorical[0]);
            o.AtrRatio = (atrHistorical[0] > 0) ? Safe(atrRecent[0] / atrHistorical[0]) : double.NaN;
            o.VolatilityPass = MLExtensions.FilterVolatility(atrRecent[0], atrHistorical[0], true);
            o.RegimeValue = SafeRegimeValue();
            o.RegimePass = regime.Evaluate(RegimeThreshold, true);
            o.AdxValue = Safe(adx[0]);
            o.AdxPass = MLExtensions.FilterAdx(adx[0], AdxThreshold, true);
            o.Ema200 = Safe(ema200[0]);
            o.DistEma200Ticks = (refPrice - ema200[0]) / TickSize;
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
            o.EntryFiltersPassed = CompositeFiltersPassed(o);
            o.FilterConfig = ActiveCompositeConfig();

            o.CandidateExecutedLive = false;
            o.ManualIntervention = false;

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

            o.IsLong = isLong;
            o.HorizonDeltaTicks = new double[HorizonMinutes.Length];
            for (int i = 0; i < HorizonMinutes.Length; i++) o.HorizonDeltaTicks[i] = double.NaN;

            // (fix 7/11) scheduled vs effective window end
            o.WindowEndScheduled = t.AddMinutes(WindowMinutes);
            o.WindowEnd = o.WindowEndScheduled;
            o.CutByRthClose = false;
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

        // ================== FORWARD TRACKING (1-tick) =====================
        private void UpdateOpenObservations(double price, DateTime tickTime)
        {
            if (openObs.Count == 0) return;

            for (int i = openObs.Count - 1; i >= 0; i--)
            {
                Observation o = openObs[i];
                if (tickTime < o.SignalTime) continue;

                // (fix 1) Boundary FIRST. A tick strictly after the effective
                // window end must NOT touch MFE/MAE/horizons/final delta.
                if (tickTime > o.WindowEnd)
                {
                    o.FinalizeReason = o.CutByRthClose ? "rth_close" : "window_complete";
                    o.RightCensored = o.CutByRthClose;
                    FinalizeAndWrite(o);
                    openObs.RemoveAt(i);
                    continue;
                }

                // In-window tick (tickTime <= WindowEnd): may affect labels.
                double favTicks = o.IsLong ? (price - o.RefPrice) / TickSize : (o.RefPrice - price) / TickSize;
                double advTicks = o.IsLong ? (o.RefPrice - price) / TickSize : (price - o.RefPrice) / TickSize;
                if (favTicks > o.MfeTicks) { o.MfeTicks = favTicks; o.MinutesToMfe = (tickTime - o.SignalTime).TotalMinutes; }
                if (advTicks > o.MaeTicks) { o.MaeTicks = advTicks; o.MinutesToMae = (tickTime - o.SignalTime).TotalMinutes; }
                o.SeenAnyTick = true;
                o.LastInWindowPrice = price;
                o.LastInWindowTime = tickTime;

                for (int h = 0; h < HorizonMinutes.Length; h++)
                    if (double.IsNaN(o.HorizonDeltaTicks[h]) && tickTime >= o.SignalTime.AddMinutes(HorizonMinutes[h]))
                        o.HorizonDeltaTicks[h] = o.IsLong ? (price - o.RefPrice) / TickSize : (o.RefPrice - price) / TickSize;

                if (tickTime == o.WindowEnd)   // exact boundary hit (already applied)
                {
                    o.FinalizeReason = o.CutByRthClose ? "rth_close" : "window_complete";
                    o.RightCensored = o.CutByRthClose;
                    FinalizeAndWrite(o);
                    openObs.RemoveAt(i);
                }
            }
        }

        private void FinalizeAndWrite(Observation o)
        {
            double finalPrice = double.IsNaN(o.LastInWindowPrice) ? o.RefPrice : o.LastInWindowPrice;
            o.FinalPrice = finalPrice;
            o.WindowEndActual = o.SeenAnyTick ? o.LastInWindowTime : o.SignalTime;
            o.FinalDeltaTicks = o.IsLong ? (finalPrice - o.RefPrice) / TickSize : (o.RefPrice - finalPrice) / TickSize;

            // (fix 2) mae_before_mfe = time_of_final_MAE < time_of_final_MFE.
            // A non-occurring excursion is treated as +infinity (never happened);
            // both absent -> false.
            double tMae = o.MaeTicks > 0 ? o.MinutesToMae : double.PositiveInfinity;
            double tMfe = o.MfeTicks > 0 ? o.MinutesToMfe : double.PositiveInfinity;
            o.MaeBeforeMfe = tMae < tMfe;

            if (signalsWriter == null) return;
            try { signalsWriter.WriteLine(BuildSignalRow(o)); }
            catch (Exception ex) { Print("SignalLogger: row write failed: " + ex.Message); }
        }

        private void FinalizeAllOpen(string reason)
        {
            for (int i = openObs.Count - 1; i >= 0; i--)
            {
                Observation o = openObs[i];
                o.RightCensored = true;
                o.FinalizeReason = reason;
                FinalizeAndWrite(o);
            }
            openObs.Clear();
        }

        // ============================ FILTERS ==============================
        private bool KernelVerdict(bool isLong, double kernel, double kernelG)
        {
            bool bullish, bearish;
            if (KernelSmoothing) { bullish = kernelG >= kernel; bearish = kernelG <= kernel; }
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
            try { return Safe(regime.NormalizedSlopeDecline); } catch { return double.NaN; }
        }

        // ========================= SESSION / TIME ==========================
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
            return t.ToString("yyyy-MM-dd", CultureInfo.InvariantCulture);
        }

        private double MinutesSinceRthOpen(DateTime t)
        {
            int h = RthStart / 100, m = RthStart % 100;
            DateTime open = new DateTime(t.Year, t.Month, t.Day, h, m, 0);
            return (t - open).TotalMinutes;
        }

        private void UpdateVwap()
        {
            DateTime t = Time[0];
            string day = t.ToString("yyyy-MM-dd");
            bool rthOpenReached = IsInSession(t, RthStart, RthEnd);
            if (vwapSessionDate.ToString("yyyy-MM-dd") != day && rthOpenReached)
            {
                vwapSessionDate = t.Date; vwapCumPV = 0.0; vwapCumV = 0.0;
            }
            if (rthOpenReached && vwapSessionDate != DateTime.MinValue)
            {
                double tp = (High[0] + Low[0] + Close[0]) / 3.0;
                double v = Volume[0];
                vwapCumPV += tp * v; vwapCumV += v;
            }
        }

        private double CurrentVwap() { return vwapCumV > 0 ? vwapCumPV / vwapCumV : double.NaN; }

        private void UpdateCandleRun()
        {
            int d = Close[0] > Open[0] ? 1 : Close[0] < Open[0] ? -1 : 0;
            if (d != 0 && d == previousCandleDirection) sameDirectionCandleRun++;
            else if (d != 0) sameDirectionCandleRun = 1;
            else sameDirectionCandleRun = 0;
            previousCandleDirection = d;
            UpdateVwap();
        }

        // ============================ OUTPUT ===============================
        private void OpenWriters()
        {
            try
            {
                instrumentFull   = (Instrument != null) ? Instrument.FullName : "UNKNOWN";
                instrumentMaster = (Instrument != null && Instrument.MasterInstrument != null) ? Instrument.MasterInstrument.Name : "UNKNOWN";
                expiryStr        = (Instrument != null && Instrument.Expiry > DateTime.MinValue) ? Instrument.Expiry.ToString("yyyy-MM-dd") : "";
                tickSizeVal      = (Instrument != null && Instrument.MasterInstrument != null) ? Instrument.MasterInstrument.TickSize : TickSize;
                try { timezoneId = (Bars != null && Bars.TradingHours != null && Bars.TradingHours.TimeZoneInfo != null) ? Bars.TradingHours.TimeZoneInfo.Id : "unknown"; }
                catch { timezoneId = "unknown"; }

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
                    barsWriter.WriteLine("run_id,instrument,instrument_master,expiry,tick_size,bar_period,bar_time,session_date,session_tag,open,high,low,close,volume");
                }

                WriteMeta();
                Print("SignalLogger: writing " + sigPath);
            }
            catch (Exception ex) { Print("SignalLogger: FAILED to open writers: " + ex.Message); }
        }

        private void WriteMeta()
        {
            try
            {
                string metaPath = Path.Combine(OutputFolder, runId + ".meta.json");
                StringBuilder sb = new StringBuilder();
                sb.AppendLine("{");
                sb.AppendLine("  \"run_id\": " + J(runId) + ",");
                sb.AppendLine("  \"created_utc\": " + J(DateTime.UtcNow.ToString("yyyy-MM-ddTHH:mm:ssZ", CultureInfo.InvariantCulture)) + ",");
                sb.AppendLine("  \"logger\": \"SignalLogger rev2\",");
                sb.AppendLine("  \"instrument_full\": " + J(instrumentFull) + ",");
                sb.AppendLine("  \"instrument_master\": " + J(instrumentMaster) + ",");
                sb.AppendLine("  \"expiry\": " + J(expiryStr) + ",");
                sb.AppendLine("  \"tick_size\": " + tickSizeVal.ToString(CultureInfo.InvariantCulture) + ",");
                sb.AppendLine("  \"bar_period\": " + J(barPeriodStr) + ",");
                sb.AppendLine("  \"timezone_id\": " + J(timezoneId) + ",");
                sb.AppendLine("  \"rth_start\": " + RthStart + ",");
                sb.AppendLine("  \"rth_end\": " + RthEnd + ",");
                sb.AppendLine("  \"classifier_session_start\": 830,");
                sb.AppendLine("  \"classifier_session_end\": 1500,");
                sb.AppendLine("  \"min_abs_score\": " + MinAbsScore + ",");
                sb.AppendLine("  \"max_abs_score\": " + MaxAbsScore + ",");
                sb.AppendLine("  \"window_minutes\": " + WindowMinutes + ",");
                sb.AppendLine("  \"cut_at_rth_close\": " + (CutAtRthClose ? "true" : "false") + ",");
                sb.AppendLine("  \"log_all_sessions\": " + (LogAllSessions ? "true" : "false") + ",");
                sb.AppendLine("  \"horizon_minutes\": [" + string.Join(",", HorizonMinutes) + "]");
                sb.AppendLine("}");
                File.WriteAllText(metaPath, sb.ToString());
                Print("SignalLogger: wrote meta " + metaPath);
            }
            catch (Exception ex) { Print("SignalLogger: meta write failed: " + ex.Message); }
        }

        private void WriteBarRow()
        {
            if (barsWriter == null || !WriteBarsFile) return;
            try
            {
                DateTime t = Time[0];
                barsWriter.WriteLine(string.Join(",",
                    runId, Q(instrumentFull), Q(instrumentMaster), expiryStr,
                    F(tickSizeVal), barPeriodStr,
                    t.ToString("yyyy-MM-ddTHH:mm:ss", CultureInfo.InvariantCulture),
                    SessionDateFor(t),
                    IsInSession(t, RthStart, RthEnd) ? "RTH" : "OVERNIGHT",
                    F(Open[0]), F(High[0]), F(Low[0]), F(Close[0]), F(Volume[0])));
            }
            catch { }
        }

        private void CloseWriters()
        {
            try { if (signalsWriter != null) { signalsWriter.Flush(); signalsWriter.Close(); signalsWriter = null; } } catch { }
            try { if (barsWriter != null) { barsWriter.Flush(); barsWriter.Close(); barsWriter = null; } } catch { }
        }

        private string SignalHeader()
        {
            StringBuilder sb = new StringBuilder();
            sb.Append("signal_id,run_id,instrument,instrument_master,expiry,tick_size,bar_period,signal_time,session_date,session_tag,");
            sb.Append("score,direction,is_long,exact_score_sequence_position,");
            sb.Append("signal_cluster_id,is_first_signal_in_cluster,bars_since_cluster_start,bars_since_last_same_dir_signal,");
            sb.Append("previous_prediction_score,score_changed_from_prior_bar,is_new_score_extreme,");
            sb.Append("signal_reference_price,");
            sb.Append("volatility_pass,atr_recent,atr_historical,atr_ratio,");
            sb.Append("regime_pass,regime_value,");
            sb.Append("adx_pass,adx_value,");
            sb.Append("ema200_pass,ema200_value,dist_ema200_ticks,dist_ema200_atr,");
            sb.Append("ema800_pass,ema800_value,dist_ema800_ticks,dist_ema800_atr,");
            sb.Append("sma200_pass,sma200_value,dist_sma200_ticks,");
            sb.Append("kernel_pass,kernel_value,kernel_gaussian,kernel_slope_ticks,price_minus_kernel_ticks,");
            sb.Append("session_pass,entry_filters_passed,filter_config,");
            sb.Append("candidate_executed_live,manual_intervention,");
            sb.Append("rsi,cci,dist_vwap_ticks,minutes_since_rth_open,minute_of_day,day_of_week,");
            sb.Append("candle_run,body_to_range,volume,volume_ratio,bar_range_ticks,");
            sb.Append("mfe_ticks,mae_ticks,minutes_to_mfe,minutes_to_mae,mae_before_mfe,");
            sb.Append("delta_1m,delta_3m,delta_5m,delta_10m,delta_15m,delta_30m,delta_60m,");
            sb.Append("final_delta_ticks,final_price,window_end_scheduled,window_end_actual,right_censored,window_minutes,finalize_reason");
            return sb.ToString();
        }

        private string BuildSignalRow(Observation o)
        {
            StringBuilder sb = new StringBuilder();
            Add(sb, Q(o.SignalId));
            Add(sb, o.RunId);
            Add(sb, Q(o.Instrument));
            Add(sb, Q(o.InstrumentMaster));
            Add(sb, o.Expiry);
            Add(sb, F(o.TickSize));
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
            Add(sb, F(o.FinalPrice));
            Add(sb, o.WindowEndScheduled.ToString("yyyy-MM-ddTHH:mm:ss", CultureInfo.InvariantCulture));
            Add(sb, o.WindowEndActual.ToString("yyyy-MM-ddTHH:mm:ss", CultureInfo.InvariantCulture));
            Add(sb, o.RightCensored ? "1" : "0");
            Add(sb, WindowMinutes.ToString(CultureInfo.InvariantCulture));
            sb.Append(o.FinalizeReason);
            return sb.ToString();
        }

        // -------- formatting helpers --------
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
            if (s.IndexOf(',') >= 0 || s.IndexOf('"') >= 0) return "\"" + s.Replace("\"", "\"\"") + "\"";
            return s;
        }
        private static string J(string s) { return "\"" + (s ?? "").Replace("\\", "\\\\").Replace("\"", "\\\"") + "\""; }

        // ======================== OBSERVATION ==============================
        private class Observation
        {
            public string SignalId, RunId, Instrument, InstrumentMaster, Expiry, BarPeriod, Direction, SessionDate, SessionTag;
            public double TickSize;
            public DateTime SignalTime;
            public int Score, SequencePosition;
            public bool IsLong;
            public int ClusterId, BarsSinceClusterStart, BarsSinceLastSameDir, PreviousBarScore;
            public bool IsFirstInCluster, ScoreChangedFromPrior, IsNewScoreExtreme;
            public double RefPrice;
            public bool VolatilityPass, RegimePass, AdxPass, Ema200Pass, Ema800Pass, Sma200Pass, KernelPass, SessionPass, EntryFiltersPassed;
            public double AtrRecent, AtrHistorical, AtrRatio, RegimeValue, AdxValue;
            public double Ema200, Ema800, Sma200, DistEma200Ticks, DistEma800Ticks, DistSma200Ticks, DistEma200Atr, DistEma800Atr;
            public double KernelValue, KernelGaussian, KernelSlopeTicks, PriceMinusKernelTicks;
            public string FilterConfig;
            public bool CandidateExecutedLive, ManualIntervention;
            public double Rsi, Cci, DistVwapTicks, MinutesSinceRthOpen, BodyToRange, Volume, VolumeRatio, BarRangeTicks;
            public int MinuteOfDay, CandleRun;
            public string DayOfWeek;
            public double MfeTicks, MaeTicks, MinutesToMfe = double.NaN, MinutesToMae = double.NaN, FinalDeltaTicks = double.NaN;
            public double[] HorizonDeltaTicks;
            public bool MaeBeforeMfe, SeenAnyTick, RightCensored, CutByRthClose;
            public DateTime WindowEnd, WindowEndScheduled, WindowEndActual, LastInWindowTime;
            public double LastInWindowPrice = double.NaN, FinalPrice = double.NaN;
            public string FinalizeReason = "";
        }
    }
}

// ═══════════════════════════════════════════════════════════════════════════
// ATSBridge — NinjaScript AddOn for ATS Trading Agent
// ═══════════════════════════════════════════════════════════════════════════
//
// This AddOn runs an HTTP server on localhost:8080 to receive order commands
// from the Python ATS Trading Agent.
//
// Installation:
//   1. Copy this file to: Documents\NinjaTrader 8\bin\Custom\AddOns\
//   2. In NinjaTrader: Tools → New NinjaScript → AddOn → (compile)
//   3. Enable the AddOn in Control Center → Tools → Options → AddOns
//
// Endpoints:
//   POST /order   — Place an order
//   POST /flatten — Flatten all positions
//   GET  /status  — Get position and account info
//
// ═══════════════════════════════════════════════════════════════════════════

#region Using declarations
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.ComponentModel.DataAnnotations;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Net;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Windows;
using System.Xml.Serialization;
using NinjaTrader.Cbi;
using NinjaTrader.Core;
using NinjaTrader.Gui;
using NinjaTrader.Gui.Tools;
using NinjaTrader.NinjaScript;
#endregion

namespace NinjaTrader.NinjaScript.AddOns
{
    public class ATSBridge : AddOnBase
    {
        // ── Configuration ───────────────────────────────────────────────────
        private const string LISTEN_PREFIX = "http://localhost:8080/";
        private const string TARGET_ACCOUNT = "MFFUEVFLX574013001";

        // ── State ───────────────────────────────────────────────────────────
        private HttpListener _listener;
        private Thread _listenerThread;
        private bool _running;
        private Account _account;
        private readonly object _lock = new object();
        private bool _started = false;

        // ── Lifecycle ───────────────────────────────────────────────────────

        protected override void OnStateChange()
        {
            if (State == State.SetDefaults)
            {
                Description = "HTTP bridge for ATS Trading Agent";
                Name = "ATSBridge";
            }
            else if (State == State.Configure)
            {
                // Nothing to configure
            }
        }

        protected override void OnWindowCreated(Window window)
        {
            // Start the HTTP listener when NinjaTrader is ready (only once)
            if (!_started)
            {
                _started = true;
                StartHttpListener();
            }
        }

        protected override void OnWindowDestroyed(Window window)
        {
            // Stop when all windows close
            if (Globals.AllWindows.Count == 0)
            {
                StopHttpListener();
                _started = false;
            }
        }

        // ── HTTP Listener ───────────────────────────────────────────────────

        private void StartHttpListener()
        {
            try
            {
                _listener = new HttpListener();
                _listener.Prefixes.Add(LISTEN_PREFIX);
                _listener.Start();
                _running = true;

                _listenerThread = new Thread(ListenLoop)
                {
                    IsBackground = true,
                    Name = "ATSBridge_HttpListener"
                };
                _listenerThread.Start();

                LogMessage("ATSBridge HTTP listener started on " + LISTEN_PREFIX);

                // Connect to the target account
                ConnectAccount();
            }
            catch (Exception ex)
            {
                LogMessage("Failed to start HTTP listener: " + ex.Message);
            }
        }

        private void StopHttpListener()
        {
            _running = false;

            if (_listener != null)
            {
                try
                {
                    _listener.Stop();
                    _listener.Close();
                }
                catch { }
                _listener = null;
            }

            LogMessage("ATSBridge HTTP listener stopped");
        }

        private void ListenLoop()
        {
            while (_running)
            {
                try
                {
                    var context = _listener.GetContext();
                    ThreadPool.QueueUserWorkItem(state => HandleRequest((HttpListenerContext)state), context);
                }
                catch (HttpListenerException)
                {
                    // Listener was stopped
                    break;
                }
                catch (Exception ex)
                {
                    if (_running)
                        LogMessage("Listener error: " + ex.Message);
                }
            }
        }

        // ── Request Routing ─────────────────────────────────────────────────

        private void HandleRequest(HttpListenerContext context)
        {
            var request = context.Request;
            var response = context.Response;

            string responseJson;
            int statusCode = 200;

            try
            {
                string path = request.Url.AbsolutePath.ToLower();
                string method = request.HttpMethod.ToUpper();

                LogMessage("Request: " + method + " " + path);

                if (path == "/order" && method == "POST")
                {
                    responseJson = HandleOrder(request);
                }
                else if (path == "/flatten" && method == "POST")
                {
                    responseJson = HandleFlatten();
                }
                else if (path == "/status" && method == "GET")
                {
                    responseJson = HandleStatus();
                }
                else if (path == "/health" && method == "GET")
                {
                    responseJson = "{\"status\":\"ok\",\"timestamp\":\"" + DateTime.UtcNow.ToString("o") + "\"}";
                }
                else
                {
                    statusCode = 404;
                    responseJson = "{\"error\":\"Not found\"}";
                }
            }
            catch (Exception ex)
            {
                statusCode = 500;
                responseJson = "{\"error\":\"" + EscapeJsonString(ex.Message) + "\"}";
                LogMessage("Request error: " + ex.Message);
            }

            // Send response
            try
            {
                byte[] buffer = Encoding.UTF8.GetBytes(responseJson);
                response.StatusCode = statusCode;
                response.ContentType = "application/json";
                response.ContentLength64 = buffer.Length;
                response.OutputStream.Write(buffer, 0, buffer.Length);
                response.OutputStream.Close();
            }
            catch { }
        }

        // ── Account Connection ──────────────────────────────────────────────

        private void ConnectAccount()
        {
            lock (_lock)
            {
                // Find the target account
                foreach (Account acct in Account.All)
                {
                    if (acct.Name == TARGET_ACCOUNT)
                    {
                        _account = acct;
                        LogMessage("Connected to account: " + _account.Name);
                        return;
                    }
                }

                // If exact match not found, try partial match
                foreach (Account acct in Account.All)
                {
                    if (acct.Name.Contains("MFFU"))
                    {
                        _account = acct;
                        LogMessage("Connected to account (partial match): " + _account.Name);
                        return;
                    }
                }

                // Fall back to first available account
                if (Account.All.Count > 0)
                {
                    _account = Account.All[0];
                    LogMessage("Warning: Using fallback account: " + _account.Name);
                }
                else
                {
                    LogMessage("No accounts available!");
                }
            }
        }

        private Account GetAccount()
        {
            lock (_lock)
            {
                if (_account == null || _account.Connection == null ||
                    _account.Connection.Status != ConnectionStatus.Connected)
                {
                    ConnectAccount();
                }
                return _account;
            }
        }

        // ── Simple JSON Parser ──────────────────────────────────────────────

        private string GetJsonStringValue(string json, string key)
        {
            // Match "key": "value" or "key":"value"
            string pattern = "\"" + Regex.Escape(key) + "\"\\s*:\\s*\"([^\"]*)\"";
            Match match = Regex.Match(json, pattern);
            return match.Success ? match.Groups[1].Value : null;
        }

        private int? GetJsonIntValue(string json, string key)
        {
            // Match "key": 123 or "key":123
            string pattern = "\"" + Regex.Escape(key) + "\"\\s*:\\s*(-?\\d+)";
            Match match = Regex.Match(json, pattern);
            if (match.Success && int.TryParse(match.Groups[1].Value, out int result))
                return result;
            return null;
        }

        private double? GetJsonDoubleValue(string json, string key)
        {
            // Match "key": 123.45 or "key":123.45
            string pattern = "\"" + Regex.Escape(key) + "\"\\s*:\\s*(-?\\d+\\.?\\d*)";
            Match match = Regex.Match(json, pattern);
            if (match.Success && double.TryParse(match.Groups[1].Value, NumberStyles.Float, CultureInfo.InvariantCulture, out double result))
                return result;
            return null;
        }

        // ── Order Handling ──────────────────────────────────────────────────

        private string HandleOrder(HttpListenerRequest request)
        {
            // Read JSON body
            string body;
            using (var reader = new StreamReader(request.InputStream, request.ContentEncoding))
            {
                body = reader.ReadToEnd();
            }

            LogMessage("Order request body: " + body);

            string action = GetJsonStringValue(body, "action")?.ToUpper();      // BUY or SELL
            string instrument = GetJsonStringValue(body, "instrument");          // e.g., MES 06-25
            int quantity = GetJsonIntValue(body, "quantity") ?? 1;
            double? stopPrice = GetJsonDoubleValue(body, "stop_price");
            string orderType = GetJsonStringValue(body, "order_type")?.ToLower() ?? "market";

            if (string.IsNullOrEmpty(action) || string.IsNullOrEmpty(instrument))
            {
                return "{\"success\":false,\"error\":\"Missing required fields: action, instrument\"}";
            }

            var account = GetAccount();
            if (account == null)
            {
                return "{\"success\":false,\"error\":\"No account connected\"}";
            }

            // Find the instrument
            Instrument ninjaInstrument = Instrument.GetInstrument(instrument);
            if (ninjaInstrument == null)
            {
                return "{\"success\":false,\"error\":\"Instrument not found: " + EscapeJsonString(instrument) + "\"}";
            }

            // Determine order action
            OrderAction orderAction = action == "BUY" ? OrderAction.Buy : OrderAction.Sell;

            try
            {
                Order order = null;

                if (orderType == "market")
                {
                    // Market order
                    order = account.CreateOrder(
                        ninjaInstrument,
                        orderAction,
                        OrderType.Market,
                        OrderEntry.Manual,
                        TimeInForce.Day,
                        quantity,
                        0,  // limit price
                        0,  // stop price
                        string.Empty,
                        "ATSBridge",
                        DateTime.MaxValue,
                        null
                    );
                }
                else if (orderType == "stop" && stopPrice.HasValue)
                {
                    // Stop order
                    order = account.CreateOrder(
                        ninjaInstrument,
                        orderAction,
                        OrderType.StopMarket,
                        OrderEntry.Manual,
                        TimeInForce.Day,
                        quantity,
                        0,  // limit price
                        stopPrice.Value,
                        string.Empty,
                        "ATSBridge",
                        DateTime.MaxValue,
                        null
                    );
                }
                else if (orderType == "limit" && stopPrice.HasValue)
                {
                    // Using stop_price as limit price for limit orders
                    order = account.CreateOrder(
                        ninjaInstrument,
                        orderAction,
                        OrderType.Limit,
                        OrderEntry.Manual,
                        TimeInForce.Day,
                        quantity,
                        stopPrice.Value,  // limit price
                        0,
                        string.Empty,
                        "ATSBridge",
                        DateTime.MaxValue,
                        null
                    );
                }
                else
                {
                    return "{\"success\":false,\"error\":\"Invalid order_type or missing stop_price: " + EscapeJsonString(orderType) + "\"}";
                }

                // Submit the order
                account.Submit(new[] { order });

                LogMessage("Order submitted: " + action + " " + quantity + " " + instrument + " @ " + orderType);

                return "{\"success\":true,\"order_id\":\"" + order.OrderId + "\",\"action\":\"" + action +
                       "\",\"instrument\":\"" + EscapeJsonString(instrument) + "\",\"quantity\":" + quantity +
                       ",\"order_type\":\"" + orderType + "\"}";
            }
            catch (Exception ex)
            {
                LogMessage("Order submission failed: " + ex.Message);
                return "{\"success\":false,\"error\":\"" + EscapeJsonString(ex.Message) + "\"}";
            }
        }

        // ── Flatten Handling ────────────────────────────────────────────────

        private string HandleFlatten()
        {
            var account = GetAccount();
            if (account == null)
            {
                return "{\"success\":false,\"error\":\"No account connected\"}";
            }

            try
            {
                // Get all positions and flatten them
                int positionsClosed = 0;

                foreach (Position position in account.Positions)
                {
                    if (position.MarketPosition != MarketPosition.Flat)
                    {
                        account.Flatten(new[] { position.Instrument });
                        positionsClosed++;
                        LogMessage("Flattened position: " + position.Instrument.FullName);
                    }
                }

                // Cancel all open orders
                int ordersCancelled = 0;
                foreach (Order order in account.Orders)
                {
                    if (order.OrderState == OrderState.Working ||
                        order.OrderState == OrderState.Accepted ||
                        order.OrderState == OrderState.Submitted)
                    {
                        account.Cancel(new[] { order });
                        ordersCancelled++;
                    }
                }

                LogMessage("Flatten complete: " + positionsClosed + " positions, " + ordersCancelled + " orders cancelled");

                return "{\"success\":true,\"positions_closed\":" + positionsClosed + ",\"orders_cancelled\":" + ordersCancelled + "}";
            }
            catch (Exception ex)
            {
                LogMessage("Flatten failed: " + ex.Message);
                return "{\"success\":false,\"error\":\"" + EscapeJsonString(ex.Message) + "\"}";
            }
        }

        // ── Status Handling ─────────────────────────────────────────────────

        private string HandleStatus()
        {
            var account = GetAccount();
            if (account == null)
            {
                return "{\"connected\":false,\"error\":\"No account connected\"}";
            }

            try
            {
                // Build position list
                var positionsList = new List<string>();
                foreach (Position position in account.Positions)
                {
                    if (position.MarketPosition != MarketPosition.Flat)
                    {
                        double posUnrealizedPnl = position.GetUnrealizedProfitLoss(PerformanceUnit.Currency);
                        positionsList.Add("{\"instrument\":\"" + EscapeJsonString(position.Instrument.FullName) +
                            "\",\"quantity\":" + position.Quantity +
                            ",\"direction\":\"" + position.MarketPosition.ToString() +
                            "\",\"avg_price\":" + position.AveragePrice.ToString("F2", CultureInfo.InvariantCulture) +
                            ",\"unrealized_pnl\":" + posUnrealizedPnl.ToString("F2", CultureInfo.InvariantCulture) + "}");
                    }
                }

                // Build open orders list
                var ordersList = new List<string>();
                foreach (Order order in account.Orders)
                {
                    if (order.OrderState == OrderState.Working ||
                        order.OrderState == OrderState.Accepted ||
                        order.OrderState == OrderState.Submitted)
                    {
                        ordersList.Add("{\"order_id\":\"" + order.OrderId +
                            "\",\"instrument\":\"" + EscapeJsonString(order.Instrument.FullName) +
                            "\",\"action\":\"" + order.OrderAction.ToString() +
                            "\",\"quantity\":" + order.Quantity +
                            ",\"order_type\":\"" + order.OrderType.ToString() +
                            "\",\"stop_price\":" + order.StopPrice.ToString("F2", CultureInfo.InvariantCulture) +
                            ",\"limit_price\":" + order.LimitPrice.ToString("F2", CultureInfo.InvariantCulture) +
                            ",\"state\":\"" + order.OrderState.ToString() + "\"}");
                    }
                }

                double cashBalance = account.Get(AccountItem.CashValue, Currency.UsDollar);
                double realizedPnl = account.Get(AccountItem.RealizedProfitLoss, Currency.UsDollar);
                double acctUnrealizedPnl = account.Get(AccountItem.UnrealizedProfitLoss, Currency.UsDollar);
                double totalEquity = account.Get(AccountItem.NetLiquidation, Currency.UsDollar);
                double buyingPower = account.Get(AccountItem.BuyingPower, Currency.UsDollar);

                return "{\"connected\":true" +
                    ",\"account_name\":\"" + EscapeJsonString(account.Name) + "\"" +
                    ",\"cash_balance\":" + cashBalance.ToString("F2", CultureInfo.InvariantCulture) +
                    ",\"realized_pnl\":" + realizedPnl.ToString("F2", CultureInfo.InvariantCulture) +
                    ",\"unrealized_pnl\":" + acctUnrealizedPnl.ToString("F2", CultureInfo.InvariantCulture) +
                    ",\"total_equity\":" + totalEquity.ToString("F2", CultureInfo.InvariantCulture) +
                    ",\"buying_power\":" + buyingPower.ToString("F2", CultureInfo.InvariantCulture) +
                    ",\"positions\":[" + string.Join(",", positionsList) + "]" +
                    ",\"open_orders\":[" + string.Join(",", ordersList) + "]" +
                    ",\"timestamp\":\"" + DateTime.UtcNow.ToString("o") + "\"}";
            }
            catch (Exception ex)
            {
                LogMessage("Status query failed: " + ex.Message);
                return "{\"connected\":true,\"error\":\"" + EscapeJsonString(ex.Message) + "\"}";
            }
        }

        // ── Helpers ─────────────────────────────────────────────────────────

        private string EscapeJsonString(string s)
        {
            if (string.IsNullOrEmpty(s)) return "";
            return s.Replace("\\", "\\\\")
                    .Replace("\"", "\\\"")
                    .Replace("\n", "\\n")
                    .Replace("\r", "\\r")
                    .Replace("\t", "\\t");
        }

        private void LogMessage(string message)
        {
            // Output to NinjaTrader Output window
            NinjaTrader.Code.Output.Process("[ATSBridge] " + message, PrintTo.OutputTab1);

            // Also write to file for debugging
            try
            {
                string logDir = Path.Combine(
                    Environment.GetFolderPath(Environment.SpecialFolder.MyDocuments),
                    "NinjaTrader 8", "logs"
                );
                if (!Directory.Exists(logDir))
                    Directory.CreateDirectory(logDir);

                string logPath = Path.Combine(logDir, "atsbridge.log");
                string logLine = DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") + " " + message + Environment.NewLine;
                File.AppendAllText(logPath, logLine);
            }
            catch { /* Ignore logging errors */ }
        }
    }
}

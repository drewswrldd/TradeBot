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
using System.IO;
using System.Linq;
using System.Net;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Xml.Serialization;
using NinjaTrader.Cbi;
using NinjaTrader.Core;
using NinjaTrader.Gui;
using NinjaTrader.Gui.Tools;
using NinjaTrader.NinjaScript;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;
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
            // Start the HTTP listener when NinjaTrader is ready
            if (_listener == null)
            {
                StartHttpListener();
            }
        }

        protected override void OnWindowDestroyed(Window window)
        {
            // Stop when the last window closes
            if (Core.Globals.AllWindows.Count == 0)
            {
                StopHttpListener();
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

                Log($"ATSBridge HTTP listener started on {LISTEN_PREFIX}", LogLevel.Information);

                // Connect to the target account
                ConnectAccount();
            }
            catch (Exception ex)
            {
                Log($"Failed to start HTTP listener: {ex.Message}", LogLevel.Error);
            }
        }

        private void StopHttpListener()
        {
            _running = false;

            if (_listener != null)
            {
                _listener.Stop();
                _listener.Close();
                _listener = null;
            }

            Log("ATSBridge HTTP listener stopped", LogLevel.Information);
        }

        private void ListenLoop()
        {
            while (_running)
            {
                try
                {
                    var context = _listener.GetContext();
                    Task.Run(() => HandleRequest(context));
                }
                catch (HttpListenerException)
                {
                    // Listener was stopped
                    break;
                }
                catch (Exception ex)
                {
                    Log($"Listener error: {ex.Message}", LogLevel.Error);
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

                Log($"Request: {method} {path}", LogLevel.Information);

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
                    responseJson = JsonConvert.SerializeObject(new { status = "ok", timestamp = DateTime.UtcNow });
                }
                else
                {
                    statusCode = 404;
                    responseJson = JsonConvert.SerializeObject(new { error = "Not found" });
                }
            }
            catch (Exception ex)
            {
                statusCode = 500;
                responseJson = JsonConvert.SerializeObject(new { error = ex.Message });
                Log($"Request error: {ex.Message}", LogLevel.Error);
            }

            // Send response
            byte[] buffer = Encoding.UTF8.GetBytes(responseJson);
            response.StatusCode = statusCode;
            response.ContentType = "application/json";
            response.ContentLength64 = buffer.Length;
            response.OutputStream.Write(buffer, 0, buffer.Length);
            response.OutputStream.Close();
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
                        Log($"Connected to account: {_account.Name}", LogLevel.Information);
                        return;
                    }
                }

                // If exact match not found, try partial match
                foreach (Account acct in Account.All)
                {
                    if (acct.Name.Contains("MFFU") || acct.Connection.Options.Name.Contains("MyFundedFutures"))
                    {
                        _account = acct;
                        Log($"Connected to account (partial match): {_account.Name}", LogLevel.Information);
                        return;
                    }
                }

                // Fall back to first available account
                if (Account.All.Count > 0)
                {
                    _account = Account.All[0];
                    Log($"Warning: Using fallback account: {_account.Name}", LogLevel.Warning);
                }
                else
                {
                    Log("No accounts available!", LogLevel.Error);
                }
            }
        }

        private Account GetAccount()
        {
            lock (_lock)
            {
                if (_account == null || _account.Connection == null || _account.Connection.Status != ConnectionStatus.Connected)
                {
                    ConnectAccount();
                }
                return _account;
            }
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

            var json = JObject.Parse(body);

            string action     = json["action"]?.ToString()?.ToUpper();      // BUY or SELL
            string instrument = json["instrument"]?.ToString();              // e.g., MES 06-25
            int    quantity   = json["quantity"]?.Value<int>() ?? 1;
            double? stopPrice = json["stop_price"]?.Value<double>();
            string orderType  = json["order_type"]?.ToString()?.ToLower() ?? "market";

            if (string.IsNullOrEmpty(action) || string.IsNullOrEmpty(instrument))
            {
                return JsonConvert.SerializeObject(new {
                    success = false,
                    error = "Missing required fields: action, instrument"
                });
            }

            var account = GetAccount();
            if (account == null)
            {
                return JsonConvert.SerializeObject(new {
                    success = false,
                    error = "No account connected"
                });
            }

            // Find the instrument
            Instrument ninjaInstrument = Instrument.GetInstrument(instrument);
            if (ninjaInstrument == null)
            {
                return JsonConvert.SerializeObject(new {
                    success = false,
                    error = $"Instrument not found: {instrument}"
                });
            }

            // Determine order action
            OrderAction orderAction = action == "BUY" ? OrderAction.Buy : OrderAction.Sell;

            try
            {
                Order order;

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
                        Core.Globals.MaxDate,
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
                        Core.Globals.MaxDate,
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
                        Core.Globals.MaxDate,
                        null
                    );
                }
                else
                {
                    return JsonConvert.SerializeObject(new {
                        success = false,
                        error = $"Invalid order_type or missing stop_price: {orderType}"
                    });
                }

                // Submit the order
                account.Submit(new[] { order });

                Log($"Order submitted: {action} {quantity} {instrument} @ {orderType}", LogLevel.Information);

                return JsonConvert.SerializeObject(new {
                    success = true,
                    order_id = order.Id.ToString(),
                    action = action,
                    instrument = instrument,
                    quantity = quantity,
                    order_type = orderType,
                    stop_price = stopPrice
                });
            }
            catch (Exception ex)
            {
                Log($"Order submission failed: {ex.Message}", LogLevel.Error);
                return JsonConvert.SerializeObject(new {
                    success = false,
                    error = ex.Message
                });
            }
        }

        // ── Flatten Handling ────────────────────────────────────────────────

        private string HandleFlatten()
        {
            var account = GetAccount();
            if (account == null)
            {
                return JsonConvert.SerializeObject(new {
                    success = false,
                    error = "No account connected"
                });
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
                        Log($"Flattened position: {position.Instrument.FullName}", LogLevel.Information);
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

                Log($"Flatten complete: {positionsClosed} positions, {ordersCancelled} orders cancelled", LogLevel.Warning);

                return JsonConvert.SerializeObject(new {
                    success = true,
                    positions_closed = positionsClosed,
                    orders_cancelled = ordersCancelled
                });
            }
            catch (Exception ex)
            {
                Log($"Flatten failed: {ex.Message}", LogLevel.Error);
                return JsonConvert.SerializeObject(new {
                    success = false,
                    error = ex.Message
                });
            }
        }

        // ── Status Handling ─────────────────────────────────────────────────

        private string HandleStatus()
        {
            var account = GetAccount();
            if (account == null)
            {
                return JsonConvert.SerializeObject(new {
                    connected = false,
                    error = "No account connected"
                });
            }

            try
            {
                // Build position list
                var positions = new List<object>();
                foreach (Position position in account.Positions)
                {
                    if (position.MarketPosition != MarketPosition.Flat)
                    {
                        positions.Add(new {
                            instrument = position.Instrument.FullName,
                            quantity = position.Quantity,
                            direction = position.MarketPosition.ToString(),
                            avg_price = position.AveragePrice,
                            unrealized_pnl = position.GetUnrealizedProfitLoss(PerformanceUnit.Currency)
                        });
                    }
                }

                // Build open orders list
                var openOrders = new List<object>();
                foreach (Order order in account.Orders)
                {
                    if (order.OrderState == OrderState.Working ||
                        order.OrderState == OrderState.Accepted ||
                        order.OrderState == OrderState.Submitted)
                    {
                        openOrders.Add(new {
                            order_id = order.Id.ToString(),
                            instrument = order.Instrument.FullName,
                            action = order.OrderAction.ToString(),
                            quantity = order.Quantity,
                            order_type = order.OrderType.ToString(),
                            stop_price = order.StopPrice,
                            limit_price = order.LimitPrice,
                            state = order.OrderState.ToString()
                        });
                    }
                }

                return JsonConvert.SerializeObject(new {
                    connected = true,
                    account_name = account.Name,
                    cash_balance = account.Get(AccountItem.CashValue, Currency.UsDollar),
                    realized_pnl = account.Get(AccountItem.RealizedProfitLoss, Currency.UsDollar),
                    unrealized_pnl = account.Get(AccountItem.UnrealizedProfitLoss, Currency.UsDollar),
                    total_equity = account.Get(AccountItem.NetLiquidation, Currency.UsDollar),
                    buying_power = account.Get(AccountItem.BuyingPower, Currency.UsDollar),
                    positions = positions,
                    open_orders = openOrders,
                    timestamp = DateTime.UtcNow
                });
            }
            catch (Exception ex)
            {
                Log($"Status query failed: {ex.Message}", LogLevel.Error);
                return JsonConvert.SerializeObject(new {
                    connected = true,
                    error = ex.Message
                });
            }
        }

        // ── Logging ─────────────────────────────────────────────────────────

        private void Log(string message, LogLevel level)
        {
            NinjaTrader.Code.Output.Process($"[ATSBridge] {message}", PrintTo.OutputTab1);

            // Also write to file for debugging
            try
            {
                string logPath = Path.Combine(
                    Environment.GetFolderPath(Environment.SpecialFolder.MyDocuments),
                    "NinjaTrader 8", "logs", "atsbridge.log"
                );
                string logLine = $"{DateTime.Now:yyyy-MM-dd HH:mm:ss} [{level}] {message}\n";
                File.AppendAllText(logPath, logLine);
            }
            catch { /* Ignore logging errors */ }
        }
    }
}

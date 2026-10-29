-- SPDX-License-Identifier: MIT
-- dv-testset Wireshark dissectors: AMBEd transcoder link, USRP, DExtra.
--
--   tshark -X lua_script:tools/wireshark/dvtest.lua -r capture.pcapng -Y ambed
--
-- AMBEd   UDP 10100 control (PING/PONG, CAPS, OPEN 17/18/19/24, STD 14/18,
--         BUSY 9/13, CS) and the per-stream data ports: the port of each STD
--         reply is followed, 10101..10199 are registered statically, and a
--         heuristic claims "AMBED" control packets on any UDP port.
-- USRP    app_rpt chan_usrp framing: 32 B header ("USRP" + 7 x BE32), voice
--         352 B (header + 320 B S16LE PCM), unkey 32 B. Ports of
--         rig/config/xlxd.usrp.in (32001-32003, 34001-34003) plus a heuristic.
-- DExtra  D-STAR DSVT header/voice and link/keepalive packets on UDP 30001.
-- MMDVM   minimal Homebrew DMR repeater link on UDP 62030 (tags, DMRD).
-- M17     minimal M17 reflector link on UDP 17000 (CONN/ACKN/PING/PONG/
--         DISC, 54 B voice).
-- NXDN    minimal NXDNReflector network link on UDP 41400 (NXDNP/NXDNU/NXDND).
-- P25     minimal P25Reflector network link on UDP 41000 (poll, LDU records,
--         terminator).
-- DPlus   minimal REF link on UDP 20001 (connect, login, keepalive, DSVT).
-- DCS     minimal DCS link on UDP 30051 (link, ACK/NAK, keepalives, voice).
-- YSF     minimal YSF reflector link on UDP 42000 (YSFP/YSFU/YSFS, YSFD;
--         the FICH is shown coded). Wireshark 4.4 has no built-in dissector
--         for any of these; IAX2 is built in and not duplicated.
--
-- All three are also available in "Decode As" (UDP port).
-- Multi-byte AMBEd stream id and port are little endian (memcpy of a host
-- uint16 on the transcoder); CAPS mask and transaction are big endian.

local udp_table = DissectorTable.get("udp.port")

----------------------------------------------------------------------------
-- AMBEd
----------------------------------------------------------------------------

local CODEC_NAMES = {
  [0] = "none",
  [1] = "AMBE+ (D-STAR)",
  [2] = "AMBE2+ (DMR/YSF)",
  [3] = "Codec2 3200 (M17)",
  [4] = "IMBE (P25)",
  [5] = "PCM S16LE 8 kHz",
}
-- payload bytes per 20 ms frame
local CODEC_WIDTH = { [1] = 9, [2] = 9, [3] = 8, [4] = 11, [5] = 320 }

local p_ambed = Proto("ambed", "AMBEd transcoder protocol")

local MSG_NAMES = {
  [1] = "PING", [2] = "PONG", [3] = "CAPS request", [4] = "CAPS reply",
  [5] = "OPEN legacy", [6] = "OPEN extended", [7] = "STD", [8] = "STD extended",
  [9] = "BUSY", [10] = "BUSY extended", [11] = "CS (close stream)",
  [20] = "data request", [21] = "data reply", [22] = "data",
}

local af = {
  msg         = ProtoField.uint8("ambed.msg", "Message", base.DEC, MSG_NAMES),
  tag         = ProtoField.string("ambed.tag", "Tag"),
  callsign    = ProtoField.string("ambed.callsign", "Callsign"),
  codec_in    = ProtoField.uint8("ambed.codec_in", "Codec in", base.DEC, CODEC_NAMES),
  codec_out   = ProtoField.uint8("ambed.codec_out", "Codec out", base.DEC, CODEC_NAMES),
  n_outs      = ProtoField.uint8("ambed.n_outs", "Outputs requested", base.DEC),
  transaction = ProtoField.uint32("ambed.transaction", "Transaction", base.HEX),
  stream_id   = ProtoField.uint16("ambed.stream_id", "Stream id", base.DEC),
  port        = ProtoField.uint16("ambed.port", "Stream data port", base.DEC),
  caps_ver    = ProtoField.uint8("ambed.caps.version", "CAPS version", base.DEC),
  caps_max    = ProtoField.uint8("ambed.caps.max_outputs", "Max outputs", base.DEC),
  caps_mask   = ProtoField.uint32("ambed.caps.mask", "Codec mask", base.HEX),
  caps_m1     = ProtoField.bool("ambed.caps.mask.ambeplus", "AMBE+ (1)", 32, nil, 0x02),
  caps_m2     = ProtoField.bool("ambed.caps.mask.ambe2plus", "AMBE2+ (2)", 32, nil, 0x04),
  caps_m3     = ProtoField.bool("ambed.caps.mask.codec2", "Codec2 3200 (3)", 32, nil, 0x08),
  caps_m4     = ProtoField.bool("ambed.caps.mask.imbe", "IMBE (4)", 32, nil, 0x10),
  caps_m5     = ProtoField.bool("ambed.caps.mask.pcm", "PCM (5)", 32, nil, 0x20),
  pid         = ProtoField.uint8("ambed.pid", "Packet id", base.DEC),
  n_entries   = ProtoField.uint8("ambed.entries", "Entries", base.DEC),
  entry       = ProtoField.none("ambed.entry", "Entry"),
  e_index     = ProtoField.uint8("ambed.entry.index", "Index", base.DEC),
  e_codec     = ProtoField.uint8("ambed.entry.codec", "Codec", base.DEC, CODEC_NAMES),
  e_payload   = ProtoField.bytes("ambed.entry.payload", "Payload"),
  e_peak      = ProtoField.uint16("ambed.entry.pcm_peak", "PCM peak |sample|", base.DEC),
  setup       = ProtoField.framenum("ambed.setup_frame", "Stream set up in frame"),
}
p_ambed.fields = af

local ae = {
  malformed = ProtoExpert.new("ambed.malformed", "Malformed AMBEd packet",
                              expert.group.MALFORMED, expert.severity.ERROR),
  unknown   = ProtoExpert.new("ambed.unknown_codec", "Unknown codec id",
                              expert.group.MALFORMED, expert.severity.ERROR),
  order     = ProtoExpert.new("ambed.order", "Reply entry does not match the opened outputs",
                              expert.group.PROTOCOL, expert.severity.WARN),
  busy      = ProtoExpert.new("ambed.busy", "Open refused (BUSY)",
                              expert.group.RESPONSE_CODE, expert.severity.NOTE),
  invalid   = ProtoExpert.new("ambed.invalid_open", "Open the reference ambed refuses",
                              expert.group.PROTOCOL, expert.severity.NOTE),
}
p_ambed.experts = { ae.malformed, ae.unknown, ae.order, ae.busy, ae.invalid }

-- Per-capture state, reset by init(). Streams are keyed by data port:
-- { codec_in, outs = {..} or nil, transcoder = address string, frame }
-- frame_stream remembers the stream of each data frame from the first pass,
-- so a later port reuse does not relabel earlier frames in the GUI.
local streams, pending_ext, pending_legacy, frame_stream

local function reset_state()
  streams, pending_ext, pending_legacy, frame_stream = {}, {}, {}, {}
end
reset_state()
p_ambed.init = reset_state

local function trim(s) return (s:gsub("[%s%z]+$", "")) end

local function outs_text(outs)
  local t = {}
  for i = 1, #outs do t[#t + 1] = tostring(outs[i]) end
  return "[" .. table.concat(t, ",") .. "]"
end

local function pcm_peak(tvb_range)
  local peak = 0
  for i = 0, tvb_range:len() - 2, 2 do
    local v = tvb_range:range(i, 2):le_int()
    if v < 0 then v = -v end
    if v > peak then peak = v end
  end
  return peak
end

-- Why an open would be refused by the reference ambed (nil = valid).
local function open_problem(cs, codec_in, slots, extended)
  if cs == "" then return "blank callsign" end
  local max_codec = extended and 5 or 4
  if codec_in < 1 or codec_in > max_codec then return "invalid input codec " .. codec_in end
  local seen, n, gap = {}, 0, false
  for i = 1, #slots do
    local c = slots[i]
    if c == 0 then
      gap = true
    else
      if gap and extended then return "output after an empty slot" end
      if c > max_codec then return "invalid output codec " .. c end
      if c == codec_in then return "output equals input" end
      if seen[c] then return "duplicate output " .. c end
      seen[c] = true
      n = n + 1
    end
  end
  if n == 0 then return "no output" end
  return nil
end

local function dissect_open(buf, pinfo, t, len)
  t:add(af.callsign, buf(7, 8))
  t:add(af.codec_in, buf(15, 1))
  -- outs: non-zero outputs (stream state); slots: wire order, trailing
  -- empty slots dropped (shown in the info column, so a gap stays visible)
  local outs, slots = {}, {}
  local last = (len == 24) and 19 or (len - 1)
  for off = 16, last do
    local c = buf(off, 1):uint()
    local item = t:add(af.codec_out, buf(off, 1))
    item:prepend_text(string.format("[%d] ", off - 16))
    slots[#slots + 1] = c
    if c ~= 0 then outs[#outs + 1] = c end
  end
  while #slots > 0 and slots[#slots] == 0 do slots[#slots] = nil end
  t:add(af.n_outs, #outs):set_generated()
  local key = tostring(pinfo.src)
  local cs = trim(buf(7, 8):string())
  local codec_in = buf(15, 1):uint()
  local problem = open_problem(cs, codec_in, slots, len == 24)
  if problem then t:add_proto_expert_info(ae.invalid, "invalid open: " .. problem) end
  local shown = (cs == "") and "''" or cs
  if len == 24 then
    t:add(af.msg, 6):set_generated()
    local tx = buf(20, 4):uint()
    t:add(af.transaction, buf(20, 4))
    pending_ext[tx] = { codec_in = codec_in, outs = outs }
    return string.format("OPEN ext %s in=%d outs=%s tx=0x%08x", shown, codec_in, outs_text(slots), tx)
  end
  t:add(af.msg, 5):set_generated()
  pending_legacy[key] = { codec_in = codec_in, outs = outs }
  return string.format("OPEN legacy(%d) %s in=%d outs=%s", len, shown, codec_in, outs_text(slots))
end

local function dissect_std(buf, pinfo, t, len)
  t:add(af.msg, (len == 18) and 8 or 7):set_generated()
  t:add_le(af.stream_id, buf(8, 2))
  t:add_le(af.port, buf(10, 2))
  t:add(af.codec_in, buf(12, 1))
  t:add(af.codec_out, buf(13, 1))
  local id, port = buf(8, 2):le_uint(), buf(10, 2):le_uint()
  local info = string.format("STD id=%d port=%d in=%d out0=%d", id, port, buf(12, 1):uint(), buf(13, 1):uint())
  local req
  if len == 18 then
    local tx = buf(14, 4):uint()
    t:add(af.transaction, buf(14, 4))
    info = info .. string.format(" tx=0x%08x", tx)
    req = pending_ext[tx]
  else
    req = pending_legacy[tostring(pinfo.dst)]
  end
  if not pinfo.visited then
    streams[port] = {
      codec_in = buf(12, 1):uint(),
      outs = req and req.outs or nil,
      transcoder = tostring(pinfo.src),
      frame = pinfo.number,
    }
    -- follow the data port when it is outside the static range
    if port < 10101 or port > 10199 then
      udp_table:add(port, p_ambed)
    end
  end
  return info
end

local function dissect_control(buf, pinfo, t)
  local len = buf:len()
  local s = buf:raw(0, math.min(len, 9))
  local info
  if s:sub(1, 9) == "AMBEDPING" then
    t:add(af.tag, buf(0, 9))
    t:add(af.msg, 1):set_generated()
    if len ~= 17 then t:add_proto_expert_info(ae.malformed, "PING is 17 bytes"); return "PING (bad length)" end
    t:add(af.callsign, buf(9, 8))
    info = "PING " .. trim(buf(9, 8):string())
  elseif s:sub(1, 9) == "AMBEDPONG" then
    t:add(af.tag, buf(0, 9))
    t:add(af.msg, 2):set_generated()
    if len ~= 9 then t:add_proto_expert_info(ae.malformed, "PONG is 9 bytes") end
    info = "PONG"
  elseif s:sub(1, 9) == "AMBEDCAPS" then
    t:add(af.tag, buf(0, 9))
    if len == 9 then
      t:add(af.msg, 3):set_generated()
      info = "CAPS request"
    elseif len == 15 then
      t:add(af.msg, 4):set_generated()
      t:add(af.caps_ver, buf(9, 1))
      t:add(af.caps_max, buf(10, 1))
      local m = t:add(af.caps_mask, buf(11, 4))
      m:add(af.caps_m1, buf(11, 4)); m:add(af.caps_m2, buf(11, 4)); m:add(af.caps_m3, buf(11, 4))
      m:add(af.caps_m4, buf(11, 4)); m:add(af.caps_m5, buf(11, 4))
      info = string.format("CAPS reply v%d max=%d mask=0x%02x", buf(9, 1):uint(), buf(10, 1):uint(), buf(11, 4):uint())
    else
      t:add_proto_expert_info(ae.malformed, "CAPS is 9 (request) or 15 (reply) bytes")
      info = "CAPS (bad length)"
    end
  elseif s:sub(1, 9) == "AMBEDBUSY" then
    t:add(af.tag, buf(0, 9))
    t:add_proto_expert_info(ae.busy)
    if len == 13 then
      t:add(af.msg, 10):set_generated()
      t:add(af.transaction, buf(9, 4))
      info = string.format("BUSY tx=0x%08x", buf(9, 4):uint())
    else
      t:add(af.msg, 9):set_generated()
      if len ~= 9 then t:add_proto_expert_info(ae.malformed, "BUSY is 9 or 13 bytes") end
      info = "BUSY"
    end
  elseif s:sub(1, 8) == "AMBEDSTD" then
    t:add(af.tag, buf(0, 8))
    if len == 14 or len == 18 then
      info = dissect_std(buf, pinfo, t, len)
    else
      t:add_proto_expert_info(ae.malformed, "STD is 14 or 18 bytes")
      info = "STD (bad length)"
    end
  elseif s:sub(1, 7) == "AMBEDOS" then
    t:add(af.tag, buf(0, 7))
    if len == 17 or len == 18 or len == 19 or len == 24 then
      info = dissect_open(buf, pinfo, t, len)
    else
      t:add_proto_expert_info(ae.malformed, "OPEN is 17, 18, 19 or 24 bytes")
      info = "OPEN (bad length)"
    end
  elseif s:sub(1, 7) == "AMBEDCS" then
    t:add(af.tag, buf(0, 7))
    t:add(af.msg, 11):set_generated()
    if len < 9 then t:add_proto_expert_info(ae.malformed, "CS is 9 bytes"); return "CS (bad length)" end
    t:add_le(af.stream_id, buf(7, 2))
    info = string.format("CS id=%d", buf(7, 2):le_uint())
  else
    t:add_proto_expert_info(ae.malformed, "unknown AMBED tag")
    info = "unknown control"
  end
  return info
end

local function add_entry(t, buf, index, coff, foff, width)
  local codec = buf(coff, 1):uint()
  local e = t:add(af.entry, buf(coff, foff + width - coff))
  e:set_text(string.format("Entry %d: %s, %d bytes", index, CODEC_NAMES[codec] or "?", width))
  e:add(af.e_index, index):set_generated()
  e:add(af.e_codec, buf(coff, 1))
  local p = buf(foff, width)
  e:add(af.e_payload, p)
  if codec == 5 then e:add(af.e_peak, pcm_peak(p)):set_generated() end
end

local function dissect_data(buf, pinfo, t)
  local len = buf:len()
  local st = frame_stream[pinfo.number]
  if st == nil then
    st = streams[pinfo.dst_port] or streams[pinfo.src_port] or false
    frame_stream[pinfo.number] = st
  end
  if st then t:add(af.setup, st.frame):set_generated() end
  local first = buf(0, 1):uint()
  if len < 2 or not CODEC_WIDTH[first] then
    t:add_proto_expert_info(ae.unknown, "first byte is not a codec id 1..5")
    return "data (bad codec)"
  end
  -- request: codec_in + pid + one payload; replies never carry codec_in
  -- (ambed refuses an output equal to the input)
  local is_request = st and first == st.codec_in
  if is_request then
    t:add(af.msg, 20):set_generated()
    t:add(af.codec_in, buf(0, 1))
    t:add(af.pid, buf(1, 1))
    if len ~= 2 + CODEC_WIDTH[first] then
      t:add_proto_expert_info(ae.malformed, string.format("request is %d bytes, expected %d", len, 2 + CODEC_WIDTH[first]))
      return "data request (bad length)"
    end
    t:add(af.e_payload, buf(2, len - 2))
    if first == 5 then t:add(af.e_peak, pcm_peak(buf(2, len - 2))):set_generated() end
    return string.format("data request in=%d pid=%d", first, buf(1, 1):uint())
  end
  t:add(af.msg, st and 21 or 22):set_generated()
  t:add(af.pid, buf(1, 1))
  local off, index, codecs = 0, 0, {}
  while off < len do
    local codec = buf(off, 1):uint()
    local width = CODEC_WIDTH[codec]
    local foff = off + ((index == 0) and 2 or 1)
    if not width then
      t:add_proto_expert_info(ae.unknown, string.format("entry %d codec %d at offset %d", index, codec, off))
      return "data reply (bad codec)"
    end
    if foff + width > len then
      t:add_proto_expert_info(ae.malformed, string.format("entry %d truncated at offset %d", index, off))
      return "data reply (truncated)"
    end
    add_entry(t, buf, index, off, foff, width)
    if st and st.outs and st.outs[index + 1] ~= codec then
      t:add_proto_expert_info(ae.order, string.format("entry %d is codec %d, open asked %s", index, codec, outs_text(st.outs)))
    end
    codecs[#codecs + 1] = codec
    off = foff + width
    index = index + 1
  end
  t:add(af.n_entries, index):set_generated()
  return string.format("%s pid=%d outs=%s", st and "data reply" or "data", buf(1, 1):uint(), outs_text(codecs))
end

function p_ambed.dissector(buf, pinfo, tree)
  if buf:len() == 0 then return 0 end
  pinfo.cols.protocol = "AMBEd"
  local t = tree:add(p_ambed, buf())
  local info
  if buf:len() >= 7 and buf:raw(0, 5) == "AMBED" then
    info = dissect_control(buf, pinfo, t)
  else
    info = dissect_data(buf, pinfo, t)
  end
  pinfo.cols.info = info
  t:append_text(", " .. info)
  return buf:len()
end

local function ambed_heur(buf, pinfo, tree)
  if buf:len() < 9 or buf:raw(0, 5) ~= "AMBED" then return false end
  p_ambed.dissector(buf, pinfo, tree)
  return true
end

----------------------------------------------------------------------------
-- USRP
----------------------------------------------------------------------------

local USRP_TYPES = {
  [0] = "voice", [1] = "DTMF", [2] = "text", [3] = "ping", [4] = "TLV",
  [5] = "voice ADPCM", [6] = "voice ulaw",
}

local p_usrp = Proto("usrp", "USRP (app_rpt chan_usrp)")
local uf = {
  eye       = ProtoField.string("usrp.eye", "Eye"),
  seq       = ProtoField.uint32("usrp.seq", "Sequence", base.DEC),
  memory    = ProtoField.uint32("usrp.memory", "Memory", base.DEC),
  keyup     = ProtoField.uint32("usrp.keyup", "Keyup", base.DEC),
  talkgroup = ProtoField.uint32("usrp.talkgroup", "Talkgroup", base.DEC),
  ftype     = ProtoField.uint32("usrp.type", "Type", base.DEC, USRP_TYPES),
  mpxid     = ProtoField.uint32("usrp.mpxid", "MPX id", base.DEC),
  reserved  = ProtoField.uint32("usrp.reserved", "Reserved", base.HEX),
  pcm       = ProtoField.bytes("usrp.pcm", "PCM (S16LE 8 kHz)"),
  peak      = ProtoField.uint16("usrp.pcm_peak", "PCM peak |sample|", base.DEC),
  payload   = ProtoField.bytes("usrp.payload", "Payload"),
}
p_usrp.fields = uf
local ue = {
  malformed = ProtoExpert.new("usrp.malformed", "Malformed USRP voice frame",
                              expert.group.MALFORMED, expert.severity.ERROR),
}
p_usrp.experts = { ue.malformed }

function p_usrp.dissector(buf, pinfo, tree)
  local len = buf:len()
  if len < 4 or buf:raw(0, 4) ~= "USRP" then return 0 end
  pinfo.cols.protocol = "USRP"
  local t = tree:add(p_usrp, buf())
  t:add(uf.eye, buf(0, 4))
  if len < 32 then
    t:add_proto_expert_info(ue.malformed, string.format("header is 32 bytes, got %d", len))
    pinfo.cols.info = "USRP (short)"
    return len
  end
  t:add(uf.seq, buf(4, 4)); t:add(uf.memory, buf(8, 4)); t:add(uf.keyup, buf(12, 4))
  t:add(uf.talkgroup, buf(16, 4)); t:add(uf.ftype, buf(20, 4)); t:add(uf.mpxid, buf(24, 4))
  t:add(uf.reserved, buf(28, 4))
  local seq, keyup, ftype = buf(4, 4):uint(), buf(12, 4):uint(), buf(20, 4):uint()
  local info
  if ftype == 0 then
    if keyup == 1 and len == 352 then
      local p = buf(32, 320)
      t:add(uf.pcm, p)
      local peak = pcm_peak(p)
      t:add(uf.peak, peak):set_generated()
      info = string.format("voice seq=%d keyup=1 peak=%d", seq, peak)
    elseif keyup == 0 and len == 32 then
      info = string.format("unkey seq=%d", seq)
    else
      t:add_proto_expert_info(ue.malformed, string.format("voice keyup=%d with %d bytes (keyed 352, unkey 32)", keyup, len))
      if len > 32 then t:add(uf.payload, buf(32, len - 32)) end
      info = string.format("voice seq=%d keyup=%d len=%d (bad)", seq, keyup, len)
    end
  else
    if len > 32 then t:add(uf.payload, buf(32, len - 32)) end
    info = string.format("%s seq=%d len=%d", USRP_TYPES[ftype] or ("type " .. ftype), seq, len)
  end
  pinfo.cols.info = info
  t:append_text(", " .. info)
  return len
end

local function usrp_heur(buf, pinfo, tree)
  if buf:len() < 32 or buf:raw(0, 4) ~= "USRP" then return false end
  p_usrp.dissector(buf, pinfo, tree)
  return true
end

----------------------------------------------------------------------------
-- DExtra (D-STAR, UDP 30001)
----------------------------------------------------------------------------

local p_dextra = Proto("dextra", "DExtra (D-STAR reflector link)")
local DX_KINDS = {
  [1] = "DSVT header", [2] = "DSVT voice", [3] = "DSVT last voice",
  [4] = "link", [5] = "unlink", [6] = "keepalive", [7] = "link ACK",
  [8] = "link NAK", [9] = "disconnected",
}
local df = {
  kind      = ProtoField.uint8("dextra.kind", "Packet", base.DEC, DX_KINDS),
  dsvt      = ProtoField.string("dextra.dsvt", "Signature"),
  ftype     = ProtoField.uint8("dextra.type", "Frame type", base.HEX, { [0x10] = "header", [0x20] = "voice" }),
  band      = ProtoField.bytes("dextra.band", "Flags/band"),
  stream_id = ProtoField.uint16("dextra.stream_id", "Stream id", base.HEX),
  seq       = ProtoField.uint8("dextra.seq", "Frame counter", base.DEC, nil, 0x1F),
  last      = ProtoField.bool("dextra.last", "Last frame", 8, nil, 0x40),
  hdr_flag  = ProtoField.uint8("dextra.header_flag", "Header marker", base.HEX),
  flags     = ProtoField.bytes("dextra.flags", "Header flags 1-3"),
  rpt2      = ProtoField.string("dextra.rpt2", "RPT2"),
  rpt1      = ProtoField.string("dextra.rpt1", "RPT1"),
  ur        = ProtoField.string("dextra.ur", "UR"),
  my        = ProtoField.string("dextra.my", "MY"),
  suffix    = ProtoField.string("dextra.suffix", "MY suffix"),
  crc       = ProtoField.uint16("dextra.crc", "CRC", base.HEX),
  ambe      = ProtoField.bytes("dextra.ambe", "AMBE frame"),
  slow      = ProtoField.bytes("dextra.slow_data", "Slow data"),
  callsign  = ProtoField.string("dextra.callsign", "Callsign"),
  module    = ProtoField.string("dextra.module", "Module"),
  rmodule   = ProtoField.string("dextra.reflector_module", "Reflector module"),
  reply     = ProtoField.string("dextra.reply", "Reply"),
}
p_dextra.fields = df
local de = {
  malformed = ProtoExpert.new("dextra.malformed", "Malformed DExtra packet",
                              expert.group.MALFORMED, expert.severity.ERROR),
}
p_dextra.experts = { de.malformed }

local function dissect_dsvt(buf, t)
  local len = buf:len()
  t:add(df.dsvt, buf(0, 4))
  t:add(df.ftype, buf(4, 1))
  t:add(df.band, buf(5, 7))
  t:add_le(df.stream_id, buf(12, 2))
  local ftype, sid = buf(4, 1):uint(), buf(12, 2):le_uint()
  if ftype == 0x10 then
    t:add(df.kind, 1):set_generated()
    if len ~= 56 then t:add_proto_expert_info(de.malformed, "DSVT header is 56 bytes"); return "DSVT header (bad length)" end
    t:add(df.hdr_flag, buf(14, 1))
    t:add(df.flags, buf(15, 3))
    t:add(df.rpt2, buf(18, 8)); t:add(df.rpt1, buf(26, 8))
    t:add(df.ur, buf(34, 8)); t:add(df.my, buf(42, 8))
    t:add(df.suffix, buf(50, 4)); t:add_le(df.crc, buf(54, 2))
    return string.format("DSVT header sid=0x%04x my=%s ur=%s rpt2=%s", sid,
      trim(buf(42, 8):string()), trim(buf(34, 8):string()), trim(buf(18, 8):string()))
  elseif ftype == 0x20 then
    local last = bit.band(buf(14, 1):uint(), 0x40) ~= 0
    t:add(df.kind, last and 3 or 2):set_generated()
    if len ~= 27 then t:add_proto_expert_info(de.malformed, "DSVT voice is 27 bytes"); return "DSVT voice (bad length)" end
    t:add(df.seq, buf(14, 1)); t:add(df.last, buf(14, 1))
    t:add(df.ambe, buf(15, 9)); t:add(df.slow, buf(24, 3))
    return string.format("DSVT %s sid=0x%04x seq=%d", last and "last" or "voice", sid, bit.band(buf(14, 1):uint(), 0x1F))
  end
  t:add_proto_expert_info(de.malformed, "DSVT frame type is 0x10 or 0x20")
  return "DSVT (unknown type)"
end

function p_dextra.dissector(buf, pinfo, tree)
  local len = buf:len()
  if len == 0 then return 0 end
  pinfo.cols.protocol = "DExtra"
  local t = tree:add(p_dextra, buf())
  local info
  if len >= 15 and buf:raw(0, 4) == "DSVT" then
    info = dissect_dsvt(buf, t)
  elseif len == 11 then
    t:add(df.callsign, buf(0, 8))
    if buf:raw(9, 1) == " " then
      t:add(df.kind, 5):set_generated()
      t:add(df.module, buf(8, 1))
      info = "unlink " .. trim(buf(0, 8):string())
    else
      t:add(df.kind, 4):set_generated()
      t:add(df.module, buf(8, 1)); t:add(df.rmodule, buf(9, 1))
      info = string.format("link %s %s->%s", trim(buf(0, 8):string()), buf(8, 1):string(), buf(9, 1):string())
    end
  elseif len == 14 and (buf:raw(10, 3) == "ACK" or buf:raw(10, 3) == "NAK") then
    local ack = buf:raw(10, 3) == "ACK"
    t:add(df.kind, ack and 7 or 8):set_generated()
    t:add(df.callsign, buf(0, 8)); t:add(df.module, buf(8, 1)); t:add(df.rmodule, buf(9, 1))
    t:add(df.reply, buf(10, 4))
    info = string.format("link %s %s", ack and "ACK" or "NAK", trim(buf(0, 8):string()))
  elseif len == 9 then
    t:add(df.kind, 6):set_generated()
    t:add(df.callsign, buf(0, 8))
    info = "keepalive " .. trim(buf(0, 8):string())
  elseif len == 12 and buf:raw(0, 12) == "DISCONNECTED" then
    t:add(df.kind, 9):set_generated()
    info = "disconnected"
  else
    t:add_proto_expert_info(de.malformed, string.format("unknown DExtra packet of %d bytes", len))
    info = "unknown"
  end
  pinfo.cols.info = info
  t:append_text(", " .. info)
  return len
end

----------------------------------------------------------------------------
-- MMDVM/Homebrew DMR (UDP 62030), minimal: tags, DMRD fields
----------------------------------------------------------------------------

local p_mmdvm = Proto("mmdvm", "MMDVM/Homebrew DMR repeater link")
local MM_FTYPES = { [0] = "voice", [1] = "voice sync", [2] = "data sync", [3] = "unused" }
local mf = {
  tag      = ProtoField.string("mmdvm.tag", "Tag"),
  rptid    = ProtoField.uint32("mmdvm.repeater_id", "Repeater id", base.DEC),
  salt     = ProtoField.uint32("mmdvm.salt", "Salt", base.HEX),
  hash     = ProtoField.bytes("mmdvm.hash", "Auth hash"),
  seq      = ProtoField.uint8("mmdvm.seq", "Sequence", base.DEC),
  src      = ProtoField.uint24("mmdvm.src", "Source id", base.DEC),
  dst      = ProtoField.uint24("mmdvm.dst", "Destination id (TG)", base.DEC),
  slot     = ProtoField.uint8("mmdvm.slot", "Slot", base.DEC, { [0] = "TS1", [1] = "TS2" }, 0x80),
  calltype = ProtoField.uint8("mmdvm.call_type", "Call type", base.DEC, { [0] = "group", [1] = "private" }, 0x40),
  ftype    = ProtoField.uint8("mmdvm.frame_type", "Frame type", base.DEC, MM_FTYPES, 0x30),
  dtype    = ProtoField.uint8("mmdvm.dtype", "Data type / voice seq", base.DEC, nil, 0x0F),
  stream   = ProtoField.uint32("mmdvm.stream_id", "Stream id", base.HEX),
  payload  = ProtoField.bytes("mmdvm.payload", "DMR payload (33 B)"),
  rest     = ProtoField.bytes("mmdvm.rest", "Trailer"),
}
p_mmdvm.fields = mf
local me = ProtoExpert.new("mmdvm.malformed", "Malformed MMDVM packet",
                           expert.group.MALFORMED, expert.severity.ERROR)
p_mmdvm.experts = { me }

-- tag, total length (nil = any), name; longest tags first
local MM_TAGS = {
  { "RPTPING", 11 }, { "MSTPONG", 11 }, { "RPTACK", nil }, { "MSTNAK", 10 },
  { "MSTCL", nil }, { "RPTCL", nil }, { "RPTL", 8 }, { "RPTK", 40 }, { "RPTC", nil },
}

function p_mmdvm.dissector(buf, pinfo, tree)
  local len = buf:len()
  if len < 4 then return 0 end
  pinfo.cols.protocol = "MMDVM"
  local t = tree:add(p_mmdvm, buf())
  local info
  if buf:raw(0, 4) == "DMRD" then
    t:add(mf.tag, buf(0, 4))
    if len ~= 55 and len ~= 53 then
      t:add_proto_expert_info(me, "DMRD is 53 or 55 bytes")
      info = "DMRD (bad length)"
    else
      t:add(mf.seq, buf(4, 1)); t:add(mf.src, buf(5, 3)); t:add(mf.dst, buf(8, 3))
      t:add(mf.rptid, buf(11, 4))
      t:add(mf.slot, buf(15, 1)); t:add(mf.calltype, buf(15, 1)); t:add(mf.ftype, buf(15, 1)); t:add(mf.dtype, buf(15, 1))
      t:add_le(mf.stream, buf(16, 4)); t:add(mf.payload, buf(20, 33))
      if len == 55 then t:add(mf.rest, buf(53, 2)) end
      local bf = buf(15, 1):uint()
      info = string.format("DMRD seq=%d %d->%d %s dt=%d sid=0x%08x", buf(4, 1):uint(), buf(5, 3):uint(),
        buf(8, 3):uint(), MM_FTYPES[bit.rshift(bit.band(bf, 0x30), 4)], bit.band(bf, 0x0F), buf(16, 4):le_uint())
    end
  else
    for _, e in ipairs(MM_TAGS) do
      local tag, want = e[1], e[2]
      if len >= #tag and buf:raw(0, #tag) == tag then
        t:add(mf.tag, buf(0, #tag))
        if want and len ~= want then t:add_proto_expert_info(me, string.format("%s is %d bytes", tag, want)) end
        if tag == "RPTACK" and len == 10 then
          t:add(mf.salt, buf(6, 4))
        elseif len >= #tag + 4 then
          t:add(mf.rptid, buf(#tag, 4))
          if tag == "RPTK" and len == 40 then t:add(mf.hash, buf(8, 32)) end
        end
        info = tag
        break
      end
    end
    if not info then
      t:add_proto_expert_info(me, "unknown MMDVM tag")
      info = "unknown"
    end
  end
  pinfo.cols.info = info
  t:append_text(", " .. info)
  return len
end

----------------------------------------------------------------------------
-- M17 reflector client (UDP 17000), minimal
----------------------------------------------------------------------------

local p_m17 = Proto("m17ref", "M17 reflector link")
local M17_ALPHA = " ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-/."
local ff = {
  tag      = ProtoField.string("m17ref.tag", "Tag"),
  callsign = ProtoField.string("m17ref.callsign", "Callsign (base-40)"),
  module   = ProtoField.string("m17ref.module", "Module"),
  sid      = ProtoField.uint16("m17ref.stream_id", "Stream id", base.HEX),
  dst      = ProtoField.string("m17ref.dst", "Destination (base-40)"),
  src      = ProtoField.string("m17ref.src", "Source (base-40)"),
  ftype    = ProtoField.uint16("m17ref.frame_type", "Frame type", base.HEX),
  meta     = ProtoField.bytes("m17ref.meta", "Meta"),
  seq      = ProtoField.uint16("m17ref.seq", "Frame number", base.DEC, nil, 0x7FFF),
  eot      = ProtoField.bool("m17ref.eot", "End of transmission", 16, nil, 0x8000),
  c2       = ProtoField.bytes("m17ref.codec2", "Codec2 3200 frame"),
  crc      = ProtoField.uint16("m17ref.crc", "CRC", base.HEX),
}
p_m17.fields = ff
local fe = ProtoExpert.new("m17ref.malformed", "Malformed M17 packet",
                           expert.group.MALFORMED, expert.severity.ERROR)
p_m17.experts = { fe }

local function b40(range)
  local v = UInt64(range:range(2, 4):uint(), range:range(0, 2):uint())
  if v == UInt64(0xFFFFFFFF, 0xFFFF) then return "@ALL" end
  local out = {}
  while v > UInt64(0) do
    local d = (v % 40):tonumber()
    out[#out + 1] = M17_ALPHA:sub(d + 1, d + 1)
    v = v / 40
  end
  return trim(table.concat(out))
end

function p_m17.dissector(buf, pinfo, tree)
  local len = buf:len()
  if len < 4 then return 0 end
  pinfo.cols.protocol = "M17"
  local t = tree:add(p_m17, buf())
  local tag = buf:raw(0, 4)
  t:add(ff.tag, buf(0, 4))
  local info
  if tag == "M17 " then
    if len ~= 54 then
      t:add_proto_expert_info(fe, "M17 voice is 54 bytes")
      info = "voice (bad length)"
    else
      t:add(ff.sid, buf(4, 2))
      t:add(ff.dst, buf(6, 6), b40(buf(6, 6))); t:add(ff.src, buf(12, 6), b40(buf(12, 6)))
      t:add(ff.ftype, buf(18, 2)); t:add(ff.meta, buf(20, 14))
      t:add(ff.seq, buf(34, 2)); t:add(ff.eot, buf(34, 2))
      t:add(ff.c2, buf(36, 8)); t:add(ff.c2, buf(44, 8)); t:add(ff.crc, buf(52, 2))
      local fn = buf(34, 2):uint()
      info = string.format("voice sid=0x%04x src=%s fn=%d%s", buf(4, 2):uint(), b40(buf(12, 6)),
        bit.band(fn, 0x7FFF), bit.band(fn, 0x8000) ~= 0 and " EOT" or "")
    end
  elseif (tag == "CONN" and len == 11) or ((tag == "PING" or tag == "PONG" or tag == "DISC") and len == 10) then
    t:add(ff.callsign, buf(4, 6), b40(buf(4, 6)))
    if tag == "CONN" then t:add(ff.module, buf(10, 1)) end
    info = tag .. " " .. b40(buf(4, 6)) .. (tag == "CONN" and (" module " .. buf(10, 1):string()) or "")
  elseif (tag == "ACKN" or tag == "NACK" or tag == "DISC") and len == 4 then
    info = tag
  else
    t:add_proto_expert_info(fe, string.format("unknown M17 packet %q of %d bytes", tag, len))
    info = "unknown"
  end
  pinfo.cols.info = info
  t:append_text(", " .. info)
  return len
end

----------------------------------------------------------------------------
-- NXDN reflector network (UDP 41400), minimal
----------------------------------------------------------------------------

local p_nxdn = Proto("nxdnref", "NXDNReflector network link")
local NX_L3 = { [0x01] = "VCALL", [0x08] = "TX_REL" }
local xf = {
  tag      = ProtoField.string("nxdnref.tag", "Tag"),
  callsign = ProtoField.string("nxdnref.callsign", "Callsign"),
  tg       = ProtoField.uint16("nxdnref.tg", "Talkgroup", base.DEC),
  src      = ProtoField.uint16("nxdnref.src", "Source id", base.DEC),
  dst      = ProtoField.uint16("nxdnref.dst", "Destination id (TG)", base.DEC),
  flags    = ProtoField.uint8("nxdnref.flags", "Flags", base.HEX),
  f_group  = ProtoField.bool("nxdnref.flags.group", "Group call", 8, nil, 0x01),
  f_data   = ProtoField.bool("nxdnref.flags.data", "Data", 8, nil, 0x02),
  f_header = ProtoField.bool("nxdnref.flags.header", "Header (VCALL)", 8, nil, 0x04),
  f_trail  = ProtoField.bool("nxdnref.flags.trailer", "Trailer (TX_REL)", 8, nil, 0x08),
  lich     = ProtoField.uint8("nxdnref.lich", "LICH", base.HEX),
  rfct     = ProtoField.uint8("nxdnref.lich.rfct", "RF channel type", base.DEC, nil, 0xC0),
  fct      = ProtoField.uint8("nxdnref.lich.fct", "Functional channel type", base.DEC, nil, 0x30),
  option   = ProtoField.uint8("nxdnref.lich.option", "Option", base.DEC, nil, 0x0C),
  outbound = ProtoField.bool("nxdnref.lich.outbound", "Outbound", 8, nil, 0x02),
  sacch    = ProtoField.bytes("nxdnref.sacch", "SACCH"),
  l3msg    = ProtoField.uint8("nxdnref.facch1.msg", "FACCH1 message", base.HEX, NX_L3, 0x3F),
  l3src    = ProtoField.uint16("nxdnref.facch1.src", "FACCH1 source id", base.DEC),
  l3dst    = ProtoField.uint16("nxdnref.facch1.dst", "FACCH1 destination id", base.DEC),
  block    = ProtoField.bytes("nxdnref.block", "Block (2 x 49-bit voice or FACCH1)"),
}
p_nxdn.fields = xf
local xe = ProtoExpert.new("nxdnref.malformed", "Malformed NXDN reflector packet",
                           expert.group.MALFORMED, expert.severity.ERROR)
p_nxdn.experts = { xe }

function p_nxdn.dissector(buf, pinfo, tree)
  local len = buf:len()
  if len < 5 then return 0 end
  pinfo.cols.protocol = "NXDN"
  local t = tree:add(p_nxdn, buf())
  local tag = buf:raw(0, 5)
  t:add(xf.tag, buf(0, 5))
  local info
  if (tag == "NXDNP" or tag == "NXDNU") and len == 17 then
    t:add(xf.callsign, buf(5, 10)); t:add(xf.tg, buf(15, 2))
    info = string.format("%s %s tg=%d", tag == "NXDNP" and "poll" or "unlink", trim(buf(5, 10):string()), buf(15, 2):uint())
  elseif tag == "NXDND" and len == 43 then
    t:add(xf.src, buf(5, 2)); t:add(xf.dst, buf(7, 2))
    local fl = t:add(xf.flags, buf(9, 1))
    fl:add(xf.f_group, buf(9, 1)); fl:add(xf.f_data, buf(9, 1)); fl:add(xf.f_header, buf(9, 1)); fl:add(xf.f_trail, buf(9, 1))
    local li = t:add(xf.lich, buf(10, 1))
    li:add(xf.rfct, buf(10, 1)); li:add(xf.fct, buf(10, 1)); li:add(xf.option, buf(10, 1)); li:add(xf.outbound, buf(10, 1))
    t:add(xf.sacch, buf(11, 4))
    local f = buf(9, 1):uint()
    local kind = (bit.band(f, 0x04) ~= 0) and "header" or ((bit.band(f, 0x08) ~= 0) and "trailer" or "voice")
    if kind ~= "voice" then
      t:add(xf.l3msg, buf(15, 1)); t:add(xf.l3src, buf(18, 2)); t:add(xf.l3dst, buf(20, 2))
    end
    t:add(xf.block, buf(15, 14)); t:add(xf.block, buf(29, 14))
    info = string.format("data %s %d->%d", kind, buf(5, 2):uint(), buf(7, 2):uint())
  else
    t:add_proto_expert_info(xe, string.format("unknown NXDN packet %q of %d bytes", tag, len))
    info = "unknown"
  end
  pinfo.cols.info = info
  t:append_text(", " .. info)
  return len
end

----------------------------------------------------------------------------
-- P25 reflector network (UDP 41000), minimal
----------------------------------------------------------------------------

local p_p25 = Proto("p25ref", "P25Reflector network link")
-- position in an LDU (0..8) -> record size, IMBE offset
local P25_SLOT = { {22, 10}, {14, 1}, {17, 5}, {17, 5}, {17, 5}, {17, 5}, {17, 5}, {17, 5}, {16, 4} }
local pf = {
  rtype    = ProtoField.uint8("p25ref.type", "Record type", base.HEX),
  kind     = ProtoField.string("p25ref.kind", "Record"),
  ldu      = ProtoField.uint8("p25ref.ldu", "LDU", base.DEC),
  slot     = ProtoField.uint8("p25ref.slot", "Voice frame in LDU", base.DEC),
  callsign = ProtoField.string("p25ref.callsign", "Callsign"),
  lcf      = ProtoField.uint8("p25ref.lcf", "LCF", base.HEX),
  mfid     = ProtoField.uint8("p25ref.mfid", "MFID", base.HEX),
  svc      = ProtoField.uint8("p25ref.service_options", "Service options", base.HEX),
  dst      = ProtoField.uint24("p25ref.dst", "Destination (TG)", base.DEC),
  src      = ProtoField.uint24("p25ref.src", "Source id", base.DEC),
  algid    = ProtoField.uint8("p25ref.algid", "ALGID", base.HEX, { [0x80] = "clear" }),
  keyid    = ProtoField.uint16("p25ref.key_id", "Key id", base.HEX),
  imbe     = ProtoField.bytes("p25ref.imbe", "IMBE frame"),
  other    = ProtoField.bytes("p25ref.other", "Other bytes (LC/ES parity, LSD, status)"),
}
p_p25.fields = pf
local pe = ProtoExpert.new("p25ref.malformed", "Malformed P25 reflector record",
                           expert.group.MALFORMED, expert.severity.ERROR)
p_p25.experts = { pe }

function p_p25.dissector(buf, pinfo, tree)
  local len = buf:len()
  if len == 0 then return 0 end
  pinfo.cols.protocol = "P25"
  local t = tree:add(p_p25, buf())
  local rt = buf(0, 1):uint()
  t:add(pf.rtype, buf(0, 1))
  local info
  if (rt == 0xF0 or rt == 0xF1) and len == 11 then
    t:add(pf.kind, rt == 0xF0 and "poll" or "unlink"):set_generated()
    t:add(pf.callsign, buf(1, 10))
    info = string.format("%s %s", rt == 0xF0 and "poll" or "unlink", trim(buf(1, 10):string()))
  elseif rt == 0x80 and len == 17 then
    t:add(pf.kind, "terminator"):set_generated()
    info = "terminator"
  elseif rt >= 0x62 and rt <= 0x73 then
    local ldu = (rt <= 0x6A) and 1 or 2
    local slot = rt - ((ldu == 1) and 0x62 or 0x6B)
    local size, off = P25_SLOT[slot + 1][1], P25_SLOT[slot + 1][2]
    t:add(pf.kind, "voice"):set_generated()
    t:add(pf.ldu, ldu):set_generated(); t:add(pf.slot, slot):set_generated()
    if len ~= size then
      t:add_proto_expert_info(pe, string.format("record 0x%02x is %d bytes, got %d", rt, size, len))
      info = string.format("LDU%d.%d (bad length)", ldu, slot)
    else
      local extra = ""
      if rt == 0x64 then
        t:add(pf.lcf, buf(1, 1)); t:add(pf.mfid, buf(2, 1)); t:add(pf.svc, buf(3, 1))
      elseif rt == 0x65 then
        t:add(pf.dst, buf(1, 3)); extra = " dst=" .. buf(1, 3):uint()
      elseif rt == 0x66 then
        t:add(pf.src, buf(1, 3)); extra = " src=" .. buf(1, 3):uint()
      elseif rt == 0x70 then
        t:add(pf.algid, buf(1, 1)); t:add(pf.keyid, buf(2, 2))
      end
      t:add(pf.imbe, buf(off, 11))
      info = string.format("LDU%d.%d%s", ldu, slot, extra)
    end
  else
    t:add_proto_expert_info(pe, string.format("unknown P25 record 0x%02x of %d bytes", rt, len))
    info = "unknown"
  end
  pinfo.cols.info = info
  t:append_text(", " .. info)
  return len
end

----------------------------------------------------------------------------
-- DPlus (REF, UDP 20001), minimal: 2-byte length/type prefix + DSVT
----------------------------------------------------------------------------

local p_dplus = Proto("dplus", "DPlus (D-STAR REF link)")
local DP_TYPES = { [0x00] = "control", [0x60] = "keepalive", [0x80] = "DV", [0xC0] = "login" }
local dpf = {
  len      = ProtoField.uint8("dplus.length", "Length", base.DEC),
  ptype    = ProtoField.uint8("dplus.type", "Type", base.HEX, DP_TYPES),
  kind     = ProtoField.string("dplus.kind", "Packet"),
  callsign = ProtoField.string("dplus.callsign", "Callsign"),
  serial   = ProtoField.string("dplus.serial", "Client id"),
  reply    = ProtoField.string("dplus.reply", "Login reply"),
  connect  = ProtoField.uint8("dplus.connect", "Connect (1) / disconnect (0)", base.DEC),
  endpat   = ProtoField.bytes("dplus.end_pattern", "End pattern"),
}
p_dplus.fields = dpf
local dpe = ProtoExpert.new("dplus.malformed", "Malformed DPlus packet",
                            expert.group.MALFORMED, expert.severity.ERROR)
p_dplus.experts = { dpe }

function p_dplus.dissector(buf, pinfo, tree)
  local len = buf:len()
  if len < 3 then return 0 end
  pinfo.cols.protocol = "DPlus"
  local t = tree:add(p_dplus, buf())
  t:add(dpf.len, buf(0, 1)); t:add(dpf.ptype, buf(1, 1))
  local plen, ptype = buf(0, 1):uint(), buf(1, 1):uint()
  local info
  if plen ~= len then
    t:add_proto_expert_info(dpe, string.format("length byte %d, packet %d bytes", plen, len))
  end
  if ptype == 0x80 and len >= 16 and buf:raw(2, 4) == "DSVT" then
    if len == 58 or len == 29 then
      t:add(dpf.kind, len == 58 and "header" or "voice"):set_generated()
      info = dissect_dsvt(buf(2):tvb(), t)
    elseif len == 32 then
      t:add(dpf.kind, "last voice"):set_generated()
      t:add(df.dsvt, buf(2, 4)); t:add(df.ftype, buf(6, 1)); t:add(df.band, buf(7, 7))
      t:add_le(df.stream_id, buf(14, 2)); t:add(df.seq, buf(16, 1)); t:add(df.last, buf(16, 1))
      t:add(dpf.endpat, buf(17, 15))
      info = string.format("DSVT last sid=0x%04x seq=%d", buf(14, 2):le_uint(), bit.band(buf(16, 1):uint(), 0x1F))
    else
      t:add_proto_expert_info(dpe, "DV is 58, 29 or 32 bytes")
      info = "DV (bad length)"
    end
  elseif ptype == 0x00 and len == 5 and buf(2, 2):uint() == 0x1800 then
    local c = buf(4, 1):uint()
    t:add(dpf.kind, c == 1 and "connect" or "disconnect"):set_generated()
    t:add(dpf.connect, buf(4, 1))
    info = c == 1 and "connect" or "disconnect"
  elseif ptype == 0x60 and len == 3 then
    t:add(dpf.kind, "keepalive"):set_generated()
    info = "keepalive"
  elseif ptype == 0xC0 and len == 28 then
    t:add(dpf.kind, "login"):set_generated()
    t:add(dpf.callsign, buf(4, 16)); t:add(dpf.serial, buf(20, 8))
    info = "login " .. trim(buf(4, 16):string())
  elseif ptype == 0xC0 and len == 8 then
    t:add(dpf.kind, "login reply"):set_generated()
    t:add(dpf.reply, buf(4, 4))
    info = "login " .. buf(4, 4):string()
  else
    t:add_proto_expert_info(dpe, string.format("unknown DPlus packet type 0x%02x, %d bytes", ptype, len))
    info = "unknown"
  end
  pinfo.cols.info = info
  t:append_text(", " .. info)
  return len
end

----------------------------------------------------------------------------
-- DCS (UDP 30051), minimal
----------------------------------------------------------------------------

local p_dcs = Proto("dcs", "DCS (D-STAR reflector link)")
local dcf = {
  kind     = ProtoField.string("dcs.kind", "Packet"),
  callsign = ProtoField.string("dcs.callsign", "Callsign"),
  own      = ProtoField.string("dcs.own_module", "Own module"),
  rmodule  = ProtoField.string("dcs.reflector_module", "Reflector module"),
  reply    = ProtoField.string("dcs.reply", "Reply"),
  refl     = ProtoField.string("dcs.reflector", "Reflector"),
  tag      = ProtoField.string("dcs.tag", "Tag"),
  flags    = ProtoField.bytes("dcs.flags", "Header flags 1-3"),
  rpt2     = ProtoField.string("dcs.rpt2", "RPT2"),
  rpt1     = ProtoField.string("dcs.rpt1", "RPT1"),
  ur       = ProtoField.string("dcs.ur", "UR"),
  my       = ProtoField.string("dcs.my", "MY"),
  suffix   = ProtoField.string("dcs.suffix", "MY suffix"),
  sid      = ProtoField.uint16("dcs.stream_id", "Stream id", base.HEX),
  seq      = ProtoField.uint8("dcs.seq", "Frame counter", base.DEC, nil, 0x1F),
  last     = ProtoField.bool("dcs.last", "Last frame", 8, nil, 0x40),
  ambe     = ProtoField.bytes("dcs.ambe", "AMBE frame"),
  slow     = ProtoField.bytes("dcs.slow_data", "Slow data"),
  counter  = ProtoField.uint24("dcs.counter", "Packet counter", base.DEC),
  text     = ProtoField.bytes("dcs.text_area", "Text area"),
}
p_dcs.fields = dcf
local dce = ProtoExpert.new("dcs.malformed", "Malformed DCS packet",
                            expert.group.MALFORMED, expert.severity.ERROR)
p_dcs.experts = { dce }

function p_dcs.dissector(buf, pinfo, tree)
  local len = buf:len()
  if len == 0 then return 0 end
  pinfo.cols.protocol = "DCS"
  local t = tree:add(p_dcs, buf())
  local info
  if len == 100 and buf:raw(0, 4) == "0001" then
    t:add(dcf.kind, "voice"):set_generated()
    t:add(dcf.tag, buf(0, 4)); t:add(dcf.flags, buf(4, 3))
    t:add(dcf.rpt2, buf(7, 8)); t:add(dcf.rpt1, buf(15, 8)); t:add(dcf.ur, buf(23, 8)); t:add(dcf.my, buf(31, 8))
    t:add(dcf.suffix, buf(39, 4)); t:add_le(dcf.sid, buf(43, 2))
    t:add(dcf.seq, buf(45, 1)); t:add(dcf.last, buf(45, 1))
    t:add(dcf.ambe, buf(46, 9)); t:add(dcf.slow, buf(55, 3))
    t:add_le(dcf.counter, buf(58, 3)); t:add(dcf.text, buf(61, 39))
    local last = bit.band(buf(45, 1):uint(), 0x40) ~= 0
    info = string.format("voice%s sid=0x%04x seq=%d n=%d my=%s", last and " last" or "", buf(43, 2):le_uint(),
      bit.band(buf(45, 1):uint(), 0x1F), buf(58, 3):le_uint(), trim(buf(31, 8):string()))
  elseif len == 519 then
    t:add(dcf.kind, "link"):set_generated()
    t:add(dcf.callsign, buf(0, 7)); t:add(dcf.own, buf(7, 1)); t:add(dcf.rmodule, buf(9, 1))
    info = string.format("link %s %s->%s", trim(buf(0, 7):string()), buf(7, 1):string(), buf(9, 1):string())
  elseif len == 14 and (buf:raw(10, 3) == "ACK" or buf:raw(10, 3) == "NAK") then
    t:add(dcf.kind, "link " .. buf:raw(10, 3)):set_generated()
    t:add(dcf.callsign, buf(0, 7)); t:add(dcf.own, buf(8, 1)); t:add(dcf.rmodule, buf(9, 1)); t:add(dcf.reply, buf(10, 4))
    info = string.format("%s %s", buf:raw(10, 3), trim(buf(0, 7):string()))
  elseif len == 11 and buf:raw(9, 1) == " " then
    t:add(dcf.kind, "unlink"):set_generated()
    t:add(dcf.callsign, buf(0, 7)); t:add(dcf.own, buf(7, 1))
    info = "unlink " .. trim(buf(0, 7):string())
  elseif len == 17 then
    t:add(dcf.kind, "keepalive (client)"):set_generated()
    t:add(dcf.callsign, buf(0, 7)); t:add(dcf.own, buf(7, 1)); t:add(dcf.refl, buf(9, 7)); t:add(dcf.rmodule, buf(16, 1))
    info = "keepalive " .. trim(buf(0, 7):string())
  elseif len == 8 or (len == 9 and buf(8, 1):uint() == 0) then
    -- reflector callsign (8), xlxd adds a 0x00
    t:add(dcf.kind, "keepalive (reflector)"):set_generated()
    t:add(dcf.refl, buf(0, 8))
    info = "keepalive " .. trim(buf(0, 8):string())
  elseif len == 22 then
    t:add(dcf.kind, "keepalive (reflector, per client)"):set_generated()
    t:add(dcf.refl, buf(0, 7)); t:add(dcf.rmodule, buf(7, 1)); t:add(dcf.callsign, buf(9, 7)); t:add(dcf.own, buf(16, 1))
    info = string.format("keepalive %s -> %s", trim(buf(0, 7):string()), trim(buf(9, 7):string()))
  else
    t:add_proto_expert_info(dce, string.format("unknown DCS packet of %d bytes", len))
    info = "unknown"
  end
  pinfo.cols.info = info
  t:append_text(", " .. info)
  return len
end

----------------------------------------------------------------------------
-- YSF reflector network (UDP 42000), minimal (FICH not decoded)
----------------------------------------------------------------------------

local p_ysf = Proto("ysfref", "YSF reflector link")
local yf = {
  tag      = ProtoField.string("ysfref.tag", "Tag"),
  kind     = ProtoField.string("ysfref.kind", "Packet"),
  callsign = ProtoField.string("ysfref.callsign", "Callsign"),
  gateway  = ProtoField.string("ysfref.gateway", "Gateway"),
  src      = ProtoField.string("ysfref.src", "Source"),
  dst      = ProtoField.string("ysfref.dst", "Destination"),
  counter  = ProtoField.uint8("ysfref.counter", "Net frame counter", base.DEC, nil, 0xFE),
  eot      = ProtoField.bool("ysfref.eot", "End of transmission", 8, nil, 0x01),
  sync     = ProtoField.bytes("ysfref.sync", "Frame sync"),
  fich     = ProtoField.bytes("ysfref.fich", "FICH (coded)"),
  payload  = ProtoField.bytes("ysfref.payload", "Payload (5 x 18 B)"),
  hash     = ProtoField.string("ysfref.status.hash", "Hash"),
  name     = ProtoField.string("ysfref.status.name", "Name"),
  desc     = ProtoField.string("ysfref.status.description", "Description"),
  count    = ProtoField.string("ysfref.status.count", "Clients"),
}
p_ysf.fields = yf
local ye = ProtoExpert.new("ysfref.malformed", "Malformed YSF packet",
                           expert.group.MALFORMED, expert.severity.ERROR)
p_ysf.experts = { ye }

function p_ysf.dissector(buf, pinfo, tree)
  local len = buf:len()
  if len < 4 then return 0 end
  pinfo.cols.protocol = "YSF"
  local t = tree:add(p_ysf, buf())
  local tag = buf:raw(0, 4)
  t:add(yf.tag, buf(0, 4))
  local info
  if tag == "YSFD" and len == 155 then
    t:add(yf.kind, "data"):set_generated()
    t:add(yf.gateway, buf(4, 10)); t:add(yf.src, buf(14, 10)); t:add(yf.dst, buf(24, 10))
    t:add(yf.counter, buf(34, 1)); t:add(yf.eot, buf(34, 1))
    t:add(yf.sync, buf(35, 5)); t:add(yf.fich, buf(40, 25)); t:add(yf.payload, buf(65, 90))
    if buf(35, 5):bytes():tohex() ~= "D471C9634D" then
      t:add_proto_expert_info(ye, "frame sync is not D4 71 C9 63 4D")
    end
    local b = buf(34, 1):uint()
    info = string.format("data n=%d%s src=%s gw=%s", bit.rshift(b, 1), bit.band(b, 1) == 1 and " EOT" or "",
      trim(buf(14, 10):string()), trim(buf(4, 10):string()))
  elseif len == 14 and buf:raw(0, 14) == "YSFPREFLECTOR " then
    t:add(yf.kind, "poll ack"):set_generated()
    info = "poll ack"
  elseif (tag == "YSFP" or tag == "YSFU") and len == 14 then
    t:add(yf.kind, tag == "YSFP" and "poll" or "unlink"):set_generated()
    t:add(yf.callsign, buf(4, 10))
    info = string.format("%s %s", tag == "YSFP" and "poll" or "unlink", trim(buf(4, 10):string()))
  elseif tag == "YSFS" and len == 4 then
    t:add(yf.kind, "status request"):set_generated()
    info = "status request"
  elseif tag == "YSFS" and len == 42 then
    t:add(yf.kind, "status"):set_generated()
    t:add(yf.hash, buf(4, 5)); t:add(yf.name, buf(9, 16)); t:add(yf.desc, buf(25, 14)); t:add(yf.count, buf(39, 3))
    info = string.format("status %s clients=%s", trim(buf(9, 16):string()), buf(39, 3):string())
  else
    t:add_proto_expert_info(ye, string.format("unknown YSF packet %q of %d bytes", tag, len))
    info = "unknown"
  end
  pinfo.cols.info = info
  t:append_text(", " .. info)
  return len
end

----------------------------------------------------------------------------
-- registration
----------------------------------------------------------------------------

udp_table:add(10100, p_ambed)
udp_table:add("10101-10199", p_ambed)
udp_table:add("32001-32003,34001-34003", p_usrp)
udp_table:add(30001, p_dextra)
udp_table:add(62030, p_mmdvm)
udp_table:add(17000, p_m17)
udp_table:add(41400, p_nxdn)
udp_table:add(41000, p_p25)
udp_table:add(20001, p_dplus)
udp_table:add(30051, p_dcs)
udp_table:add(42000, p_ysf)
for _, p in ipairs({ p_ambed, p_usrp, p_dextra, p_mmdvm, p_m17, p_nxdn, p_p25, p_dplus, p_dcs, p_ysf }) do
  udp_table:add_for_decode_as(p)
end
p_ambed:register_heuristic("udp", ambed_heur)
p_usrp:register_heuristic("udp", usrp_heur)

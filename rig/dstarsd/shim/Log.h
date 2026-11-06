// SPDX-License-Identifier: MIT
// Our stand-in for MMDVMHost's Log.h (the upstream one pulls in a JSON library and an
// MQTT client that the slow data decoder does not need). Only the logging macros the
// copied MMDVMHost files use; Log() itself is defined in oracle-mmdvm.cpp (stderr).
#if !defined(LOG_H)
#define LOG_H

#define LogDebug(fmt, ...)   Log(1U, fmt, ##__VA_ARGS__)
#define LogMessage(fmt, ...) Log(2U, fmt, ##__VA_ARGS__)
#define LogInfo(fmt, ...)    Log(3U, fmt, ##__VA_ARGS__)
#define LogWarning(fmt, ...) Log(4U, fmt, ##__VA_ARGS__)
#define LogError(fmt, ...)   Log(5U, fmt, ##__VA_ARGS__)
#define LogFatal(fmt, ...)   Log(6U, fmt, ##__VA_ARGS__)

extern void Log(unsigned int level, const char* fmt, ...);

#endif

// SPDX-License-Identifier: MIT
// Driver for DStarGateway's slow data collectors (GPL-2.0-or-later, linked unmodified at
// image build time, run as a process; docs/dstar-slowdata-cases.md): CTextCollector,
// CGPSACollector ($$CRC, D-PRS) and CNMEASentenceCollector for the six sentence types
// CAPRSCollector registers. The calls follow Common/RepeaterHandler.cpp (text: every
// frame goes to writeData(), which handles the sync itself) and Common/APRSHandler.cpp
// (GPS: sync -> sync(), other frames -> writeData(); on true: getData(string) then reset()).
//
// Input lines:   B <name> / F <seq> <hex6> / E        as oracle-mmdvm.cpp
// Output lines:  R text <seq> <hex>                   20 bytes
//                R gpsa <seq> <hex>                   the APRS line after "$$CRCxxxx,"
//                R nmea:<id> <seq> <hex>              the APRS line DStarGateway builds
#include "TextCollector.h"
#include "GPSACollector.h"
#include "NMEASentenceCollector.h"
#include "AMBEData.h"

#include <cstdio>
#include <cstring>
#include <memory>
#include <string>
#include <vector>

static void printStr(const std::string& kind, unsigned int seq, const std::string& s)
{
	std::printf("R %s %u ", kind.c_str(), seq);
	for (unsigned char ch : s)
		std::printf("%02x", ch);
	std::printf("\n");
}

struct Named {
	std::string name;
	std::unique_ptr<CSlowDataCollector> collector;
};

int main()
{
	CTextCollector text;
	std::vector<Named> sentences;
	sentences.push_back({"gpsa", std::make_unique<CGPSACollector>()});
	for (const char* id : {"$GPGGA", "$GPGLL", "$GPVTG", "$GPRMC", "$GPGSA", "$GPGSV"})
		sentences.push_back({std::string("nmea:") + (id + 1), std::make_unique<CNMEASentenceCollector>(id)});

	auto resetAll = [&]() {
		text.reset();
		for (auto& s : sentences) {
			s.collector->reset();
			s.collector->setMyCall1("N0CALL  ");
			s.collector->setMyCall2("    ");
		}
	};
	resetAll();

	char line[256];
	while (std::fgets(line, sizeof(line), stdin) != nullptr) {
		if (line[0] == 'B') {
			resetAll();
			std::printf("D %s", line + 2);
		} else if (line[0] == 'F') {
			unsigned int seq = 0U, a = 0U, b = 0U, c = 0U;
			if (std::sscanf(line, "F %u %2x%2x%2x", &seq, &a, &b, &c) != 4)
				continue;
			unsigned char slow[3U] = {(unsigned char)a, (unsigned char)b, (unsigned char)c};
			CAMBEData data((unsigned char)seq, slow);

			text.writeData(data);
			if (text.hasData())
				printStr("text", seq, text.getData());

			if (data.isSync()) {
				for (auto& s : sentences)
					s.collector->sync();
			} else {
				for (auto& s : sentences) {
					if (!s.collector->writeData(slow))
						continue;
					std::string out;
					if (s.collector->getData(out))
						printStr(s.name, seq, out);
					s.collector->reset();
					s.collector->setMyCall1("N0CALL  ");
					s.collector->setMyCall2("    ");
				}
			}
		} else if (line[0] == 'E') {
			std::printf("D end\n");
		}
		std::fflush(stdout);
	}
	return 0;
}

// SPDX-License-Identifier: MIT
// Our stand-in for DStarGateway's AMBEData.h, which drags in the network stack. The
// unmodified TextCollector.cpp uses exactly two members of CAMBEData: isSync() and
// getData(). The 12 byte frame is 9 voice bytes (zeros here) and the 3 byte slow data
// field; isSync() is the same test as DStarBase/AMBEData.cpp ((sequence & 0x1F) == 0).
#pragma once

#include <cstring>

class CAMBEData {
public:
	CAMBEData(unsigned char seq, const unsigned char* slow3) :
	m_seq(seq)
	{
		std::memset(m_data, 0x00, sizeof(m_data));
		std::memcpy(m_data + 9U, slow3, 3U);
	}

	bool isSync() const { return (m_seq & 0x1FU) == 0x00U; }

	unsigned int getData(unsigned char* data, unsigned int) const
	{
		std::memcpy(data, m_data, 12U);
		return 12U;
	}

private:
	unsigned char m_seq;
	unsigned char m_data[12];
};

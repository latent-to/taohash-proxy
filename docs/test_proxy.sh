  #!/bin/bash

  # Colors for output
  GREEN='\033[0;32m'
  RED='\033[0;31m'
  YELLOW='\033[1;33m'
  NC='\033[0m'

  echo -e "${YELLOW}Testing TaoHash Mining Proxy Connections...${NC}\n"

  # Test port 3331 (Normal Difficulty)
  echo -e "${YELLOW}Testing Port 3331 (Normal Difficulty)...${NC}"
  response_3331=$(echo '{"id": 1, "method": "mining.subscribe", "params": []}' | nc -w 3 localhost 3331 2>/dev/null)

  if [ -n "$response_3331" ]; then
      echo -e "${GREEN}✓ Port 3331 is responding${NC}"
      echo "Response: $response_3331"
  else
      echo -e "${RED}✗ Port 3331 is not responding or connection failed${NC}"
  fi

  echo ""

  # Test port 3332 (High Difficulty)
  echo -e "${YELLOW}Testing Port 3332 (High Difficulty)...${NC}"
  response_3332=$(echo '{"id": 1, "method": "mining.subscribe", "params": []}' | nc -w 3 localhost 3332 2>/dev/null)

  if [ -n "$response_3332" ]; then
      echo -e "${GREEN}✓ Port 3332 is responding${NC}"
      echo "Response: $response_3332"
  else
      echo -e "${RED}✗ Port 3332 is not responding or connection failed${NC}"
  fi

  echo -e "\n${YELLOW}Connection Test Complete!${NC}"
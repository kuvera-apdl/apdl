import { APDL, init, type APDLApi } from '@apdl-oss/sdk';
import { APDLProvider, useAPDL } from '@apdl-oss/sdk/react';

const client: APDLApi = APDL.init({
  endpoint: 'https://apdl.invalid',
  auth: { clientKey: 'client_contract_0123456789abcdef' },
});

void client;
void init;
void APDLProvider;
void useAPDL;

import sdk = require('@apdl-oss/sdk');
import reactSdk = require('@apdl-oss/sdk/react');

const client: sdk.APDLApi = sdk.APDL.init({
  endpoint: 'https://apdl.invalid',
  auth: { clientKey: 'client_contract_0123456789abcdef' },
});

void client;
void sdk.init;
void reactSdk.APDLProvider;
void reactSdk.useAPDL;

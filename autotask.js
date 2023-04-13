exports.handler = async function (payload) {
  const conditionRequest = payload.request.body;
  const matches = [];
  const events = conditionRequest.events;
  for (const evt of events) {
    
    //transaction timestamp
    let timeStamp = evt.timestamp; 
	  //decoded queryData
    let hexString = evt.matchReasons[0].args[3]; // each transaction's query data
    hexString = hexString.replace(/04/g, ''); // remove special characters in data
    hexString = hexString.replace(/08/g, '');
    hexString = hexString.replace(/0c/g, '');                                     
    const uint8Array = new Uint8Array(hexString.match(/.{1,2}/g).map(byte => parseInt(byte, 16))); // convert hex string to a Uint8Array of bytes
    const asciiString = new TextDecoder().decode(uint8Array); // convert bytes to an ASCII string
 
    // metadata 
    matches.push({
      metadata: {
        timestamp: timeStamp,
        queryId: evt.matchReasons[0].args[0],
        value: (Math.round((parseInt(evt.matchReasons[0].args[1], 16) / 1e18) * 100 ) / 100).toFixed(2),
        queryData: evt.matchReasons[0].args[3],
        decodedData: asciiString,
      },
    });
  }
  return { matches };
};
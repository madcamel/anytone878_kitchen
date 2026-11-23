// Remove functions (and clear their code back to undefined bytes) at the addresses given
// as script arguments, e.g. junk created by a bad heuristic. Prints what it removed.
//@category Ham
import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.listing.Function;

public class RemoveFunctionsAt extends GhidraScript {
	@Override
	public void run() throws Exception {
		for (String a : getScriptArgs()) {
			Address addr = toAddr(Long.parseLong(a, 16));
			Function f = getFunctionAt(addr);
			if (f == null) {
				println("no function at " + addr);
				continue;
			}
			var body = f.getBody();
			removeFunction(f);
			clearListing(body);
			println("removed " + addr + " (" + body.getNumAddresses() + " bytes cleared)");
		}
	}
}

// Create Thumb functions at the given addresses, or at every pointer of a table:
//   args: a1 a2 ...            (hex, thumb bit ignored)
//   args: table <addr> <stride> <ptr_off> <count>
// Use for code reached only through data tables (menu vtables etc.), which auto-analysis misses.
//@category Ham
import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.lang.Register;
import ghidra.program.model.listing.Function;
import java.math.BigInteger;
import java.util.*;

public class CreateFunctionsAt extends GhidraScript {
	@Override
	public void run() throws Exception {
		String[] a = getScriptArgs();
		List<Long> targets = new ArrayList<>();
		if (a.length > 0 && a[0].equals("table")) {
			long t = Long.parseLong(a[1], 16); int stride = Integer.parseInt(a[2]);
			int off = Integer.parseInt(a[3]); int n = Integer.parseInt(a[4]);
			for (int i = 0; i < n; i++)
				targets.add(getInt(toAddr(t + (long) i * stride + off)) & 0xffffffffL);
		} else for (String s : a) targets.add(Long.parseLong(s, 16));
		Register tmode = currentProgram.getRegister("TMode");
		int made = 0;
		for (long v : targets) {
			Address addr = toAddr(v & ~1L);
			if (getFunctionAt(addr) != null) continue;
			Function c = getFunctionContaining(addr);
			if (c != null) { println("inside " + c.getName() + ": " + addr); continue; }
			if (getInstructionAt(addr) == null) {
				clearListing(addr, addr.add(1));
				currentProgram.getProgramContext().setValue(tmode, addr, addr, BigInteger.ONE);
				disassemble(addr);
			}
			Function f = createFunction(addr, null);
			println((f != null ? "created " : "FAILED ") + addr);
			if (f != null) made++;
		}
		println("made " + made);
	}
}
